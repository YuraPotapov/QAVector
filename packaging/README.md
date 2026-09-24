# Packaging

Builds an installable QAVector for someone who has no Python, no pip,
no git and no interest in any of them. They get one file, install it, and find the
app in their launcher.

### Linux (.deb)

```
./packaging/build_deb.sh              # -> installers/linux/<version>/
./packaging/build_deb.sh --keep-venv  # reuse the build venv; much faster
```

Needed on the **build** machine: `python3` with `venv`, `dpkg-deb`, `fakeroot`.
Needed on the **target** machine: nothing but Ubuntu 22.04 and Google Chrome.

### Windows (setup .exe)

**This has to run on Windows.** PyInstaller does not cross-compile, so there is no
way to produce the `.exe` from a Linux checkout. It wants a Windows machine, a VM,
or a `windows-latest` CI runner.

#### 1. On the Windows machine, install

- [Python 3.10+](https://www.python.org/downloads/) — tick **"Add python.exe to
  PATH"** in the installer. The build looks for `python`; a machine with only the
  `py` launcher stops with a message saying so.
- [Inno Setup 6](https://jrsoftware.org/isdl.php) — only for the installer step.
  `-NoInstaller` skips it and leaves the two frozen bundles.
- Google Chrome — the build's own health check reports which browser it found.

#### 2. Get the source there

```powershell
git clone <this repo>
cd chrome-multi-session
git checkout <the branch you are building>
```

#### 3. Build

One command. The other two lines are variants of it, not further steps.

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1

.\packaging\build_exe.ps1 -KeepVenv      # later builds: skips ~200 MB of installs
.\packaging\build_exe.ps1 -NoInstaller   # freeze only, no Inno Setup
```

`-ExecutionPolicy Bypass` is not optional on a default Windows install, which
refuses to run unsigned `.ps1` files — without it the build fails before it starts
with "cannot be loaded because running scripts is disabled on this system".

The script resolves everything from its own location, so it does not care which
directory you run it from.

#### What it produces

```
installers\windows\<version>\
  qavector-<version>-setup.exe
  SHA256SUMS
```

The installer offers a per-user install when it is not elevated, because a locked
down QA machine is a common place to want this and needing an administrator is a
reason not to bother. Chrome's absence is reported at install time, where it is
still actionable, rather than at first launch.

#### When it fails the first time

Nothing here has been run on Windows yet — it was written against the `.deb`
build and checked as far as Linux can check it (the PowerShell parses, the `.iss`
has no dangling string concatenation, and the shared health check still passes for
the `.deb`). The likely stumbles, in the order they would appear:

1. **A missing hidden import.** The PyInstaller specs were tuned against Linux
   PySide6. Step 4's health check exists to catch exactly this, and names what is
   missing rather than shipping a bundle that fails on the user's machine.
2. **`node.exe` not where expected.** The build asserts Playwright's driver
   arrived at `driver\node.exe`; if the Windows layout differs it stops there
   instead of producing an installer that cannot run a flow.
3. **The job object.** `session_launcher._windows_kill_on_close_job` builds its
   `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` from the Win32 headers and has never met
   a real kernel. A wrong layout fails safely — a debug line, and windows that may
   outlive a hard kill of the launcher — so it cannot break the build.

## What comes out

```
installers/linux/0.15.0/
  qavector_0.15.0_amd64.deb   the canonical Debian name
  qavector_amd64.deb          the same file, under the short name
  SHA256SUMS
```

`installers/<platform>/<version>/` is versioned so several releases can sit side by
side. The `.deb` files themselves are git-ignored — they are ~96 MB — but the
directories and their `SHA256SUMS` are committed, so the history records what was
released and with which checksum.

## Installing

```bash
sudo apt install ./qavector_amd64.deb
```

`apt`, not `dpkg -i`: the package depends on the X, GL and xkb libraries that Qt
loads at runtime, and apt is what resolves them. `sudo dpkg -i qavector_amd64.deb`
works too on a normal desktop, where those libraries are already present.

Then: **QAVector** in the applications menu, or `qavector`
in a terminal for the CLI.

## What gets installed where

| Path | What |
|---|---|
| `/opt/qavector/core/` | the launcher, frozen (Python, cryptography, pyyaml, playwright + its Node driver, `flows/`, `extensions/`, `hud.js`) |
| `/opt/qavector/gui/` | the PySide6 front-end, frozen |
| `/usr/bin/qavector{,-gui}` | wrappers onto the two bundles |
| `/usr/share/applications/…desktop` | the launcher entry |
| `~/QAVector/` | **the user's**: `users.json`, `user_sessions/`, `reports/`, `flows/` |
| `~/.local/share/qavector/gui/` | the GUI's run history and saved configurations |

The split is the point. Everything under `/opt` is replaced wholesale on upgrade;
nothing under `~` is ever touched by dpkg. `runtime_paths.py` is what draws the
line, and `tests/test_runtime_paths.py` is what keeps it drawn.

## Upgrading

```bash
sudo apt install ./qavector_amd64.deb    # the new one, over the old one
```

Upgrading from **chrome-multi-session** (QAVector's name before 0.15.0) is the
same command: the package replaces that one. The first start then moves
`~/ChromeMultiSession` to `~/QAVector` and the GUI's own folders to their new
names, leaving a link at each old path, so anything that still points there -
a script in `services.json`, a desktop link - keeps working.

No uninstall, no reinstalling dependencies, no recreating configuration. Sessions,
credentials, reports and GUI history all survive, because dpkg never created them.

## Design notes

**Two executables, not one.** The GUI spawns the core as a subprocess and parses
its `--describe` / `--events` output; that boundary is the architecture, not an
accident, so packaging keeps it. `cms_gui.core.frozen_core()` finds the core
executable next to the GUI's, and the `.deb` wrapper also names it explicitly in
`CMS_CORE_EXE`.

**onedir, not onefile.** The GUI spawns the core for every `--describe` and every
run. A onefile build would unpack a 200 MB archive into `/tmp` each time.

**Two flows trees.** The bundled `flows/` is read-only - it lives in the
PyInstaller bundle and is replaced wholesale on upgrade - so anything written by
the Scenarios page or the recorder goes to `~/QAVector/flows`, which is
searched *first*. A scenario there shadows a bundled one of the same id and can
still `use:` the shipped blocks without copying them; `selectors.yaml` is merged
rather than replaced. `--flows-dir` still means exactly the directory it names -
only the default is layered.

**No bundled Chromium.** The adapter only ever `connect_over_cdp`s to the Chrome
already on the machine, so `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` is set for the
build and ~400 MB never gets downloaded. Playwright's **Node driver** is a
different matter and *is* bundled — `connect_over_cdp` goes through it — at
`_internal/playwright/driver/`, which is the one path
`playwright._impl._driver.compute_driver_executable()` will look in.

**`LD_LIBRARY_PATH`.** PyInstaller points it at the bundle, and children inherit
it. Chrome started that way loads our `libssl` and dies, so every subprocess that
runs a foreign binary goes through `runtime_paths.clean_subprocess_env()`.

**Chrome is a Suggests, not a Recommends.** apt installs Recommends by default,
and on Ubuntu 22.04 `chromium-browser` is a 2 KB shim that only redirects to a
snap — installing it would put something on `PATH` that looks like a browser and
launches nothing. So the package suggests `google-chrome-stable` and says nothing
else; `find_chrome()` prefers a binary that actually answers `--version`, and both
`postinst` and the GUI's first `--describe` tell the user how to install one.

**Sizes.** The GUI now bundles Qt WebEngine for styled HTML artifact previews,
including its renderer process and resources. Qt Quick Widgets prepares the
window's graphics composition before it appears, so opening the first HTML
report does not recreate the window. Earlier bundle size estimates predate
these additions. The exclusion list in `gui.spec` still drops unused Quick
Controls, 3D and multimedia modules.

## Building for Windows

Not built here — PyInstaller does not cross-compile, and this is a Linux
workstation. The runtime work Windows needs is already done and tested:
`runtime_paths` resolves `%USERPROFILE%\QAVector` and reads `$CMS_HOME`,
`find_chrome()` reads the `App Paths` registry key and probes Program Files,
`session_dir_for()` produces NTFS-legal profile names, `seed_password()` steps
aside where Chrome's password store cannot be written from outside, and
`cms_gui.core` already handles a `.exe` core.

What remains is packaging only:

1. `packaging/pyinstaller/{core,gui}-win.spec` — the same two specs plus
   `icon=` from `python -m cms_gui.icon` (it already writes a multi-size `.ico`).
2. `packaging/windows/installer.iss` — Inno Setup, with a constant `AppId` GUID
   and `UsePreviousAppDir=yes` so a new version installs over the old one; a
   project-directory picker that writes `data_dir` into `<InstallDir>\cms.ini`
   (read by `runtime_paths.configured_data_root`, second after `$CMS_HOME` and
   before the `%USERPROFILE%` default); the Desktop-shortcut task; and a
   `[Code]` check for the Chrome registry key. **Done.**
3. `packaging/windows/build.ps1`, run on a machine with Python 3.11+ and Inno
   Setup 6 — or a `windows-latest` job that calls it.

Output belongs in `installers/windows/<version>/`.
