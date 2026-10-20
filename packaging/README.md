# Packaging

Builds an installable QAVector for someone who has no Python, no pip,
no git and no interest in any of them. They get one file, install it, and find the
app in their launcher.

### Linux (.deb)

```
./packaging/build_deb.sh              # -> installers/linux/<version>/
./packaging/build_deb.sh --keep-venv  # reuse the build venv; much faster
```

Needed on the **build** machine: `python3` with `venv`, `dpkg-deb`, `fakeroot`,
C toolchain, `pkg-config`, libcap development files and `readelf`. The script
installs pinned Meson/Ninja in its build venv and builds pinned Bubblewrap source.
The target does not install system bubblewrap. Runtime resources include private
libcap, checksums, source, licenses and inventory; see
[ER-BB-001 build and compatibility contract](../docs/bundled-bubblewrap.md).
Target baseline: Linux x86_64 with glibc 2.35+, the declared DEB dependencies
(installed by apt) and Google Chrome. Ubuntu 22.04 is the build/acceptance baseline;
the runtime checks ABI/kernel capabilities rather than the distribution name.

### Third-party license release gate (ER-LIC-001)

Both PyInstaller specs and installer scripts require the offline Bubblewrap
NOTICE, original copyright notices and verbatim LGPL 2.1 text. Linux additionally
ships the exact source archive, independently usable build scripts/configuration,
and the private libcap version/copyright/BSD license. The corresponding build
materials are redistributable under LGPL-2.1-or-later; independent application
source retains its existing license.

```bash
python3 packaging/check_licenses.py --licenses=licenses
python3 packaging/check_licenses.py --runtime=build/dist/core/_internal/runtime --licenses=build/dist/gui/_internal/licenses
```

The second check compares actual core and GUI materials before DEB creation.
The GUI offers **Help → About → Third-Party Licenses** without the core or
internet. Windows includes offline reference notices without Linux executables.
Wheel/sdist include static notices. There is currently no AppImage build; any
new distribution format must copy these materials and run this gate before
publishing. See [ER-LIC-001](../docs/QAVector_Bubblewrap_License_Requirement.md).

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
# Portable Execution Runtime preview

To build current GUI and core together without replacing the installed application:

```sh
build/venv/bin/python packaging/build_runtime_preview.py \
  --runtime-distribution /path/to/reviewed-bundled-runtime \
  --output build/runtime-preview
build/runtime-preview/launch-qavector.sh
```

The output must be a new directory. Both frozen components include the complete
offline runtime notices; the builder runs ER-LIC-001 gates and bundled core
diagnostics. It uses the existing build environment and requires a previously
built, pinned Bubblewrap distribution. Windows provisioning remains a stub.

## Offline DEB candidate from a reviewed preview

Use a preview with its `BUILD-INFO.json` and the exact reviewed source tree:

```sh
build/venv/bin/python packaging/package_runtime_preview.py \
  --preview build/runtime-claude-setup-20261019 \
  --icons build/icons --output build/runtime-release-candidate
```

Output must be new. This path reuses the reviewed frozen components, does not
download dependencies, install the package, execute maintainer scripts or change
user definitions. It verifies binary/source hashes and mandatory Bubblewrap/
libcap attribution, complete licenses and corresponding source. The staged
package is checked again immediately before archiving and after extraction;
all payload files, modes and symlinks must match its manifest. An extracted-core
smoke uses a temporary profile and never invokes host `/opt` wrappers.

The candidate directory contains the DEB, `SHA256SUMS` and `PACKAGE-INFO.json`.
This is archive/extraction acceptance; actual install/upgrade/removal and target
dependency compatibility still require an isolated test system. No application
publication or system installation happens as part of this command. Windows
native runtime is outside this Linux packaging workflow.

## Offline isolated CPU and package lifecycle acceptance

These Linux-only harnesses use already cached local Docker images. They reject
remote Docker endpoints and existing output directories, never pull images and
disable container networking. They do not mount host user data or existing
volumes, install host packages, execute Cycles/Services or call a provider.
Choose reviewed standard images by immutable ID:

```sh
.venv/bin/python packaging/acceptance/prepare_dependencies.py \
  --image sha256:REVIEWED_UBUNTU_BASE_ID --output build/qa-public-dependencies \
  installers/linux/0.16.28/qavector_0.16.28_amd64.deb \
  build/runtime-release-candidate/qavector_0.16.29_amd64.deb
.venv/bin/python packaging/accept_runtime_install.py \
  --image sha256:REVIEWED_UBUNTU_BASE_ID --dependencies build/qa-public-dependencies \
  --previous installers/linux/0.16.28/qavector_0.16.28_amd64.deb \
  --candidate build/runtime-release-candidate/qavector_0.16.29_amd64.deb \
  --output build/qa-install-acceptance
.venv/bin/python packaging/accept_runtime_cpu.py \
  --image sha256:REVIEWED_PYTHON_BASE_ID \
  --bundle /path/to/reviewed-bundled-runtime --output build/qa-resource-acceptance
```

The dependency builder requires installed `dpkg-repack` and repacks the selected
public library dependency closure from the host. Its provenance is explicitly
recorded; this is distinct from acceptance against a clean package repository.
The installer runs real dpkg install/upgrade/remove/purge, checks synthetic data
hashes/modes/ownership, starts the installed core/GUI as UID 1000 with a temporary
profile and verifies offline license/source readability. GUI startup uses Qt's
offscreen platform; it does not establish desktop interaction coverage.

The resource harness requires local rootful Docker with the systemd cgroup v2
layout and CPU already enabled in Docker's parent. It does not configure host
controllers. It builds a bounded static public probe using `cc`, copies a static
`/usr/bin/busybox` and public libseccomp, and exercises the real LinuxBackend.
Only the newly owned anchor scope is bind-mounted writable. The runner uses
SETFCAP and unconfined seccomp/AppArmor/systempaths for nested Bubblewrap, shares
only its anchor's PID namespace, and has no privileged/SYS_ADMIN/host PID access.
The native workload retains its own isolation checks and drops all capabilities.
These runner settings are recorded in `isolation.json`.

Both drivers verify ownership labels before removing their own containers and
retain logs on failure. Resource input hashes, actual kernel metrics, dependency
origin/versions/hashes, package hashes and preservation evidence accompany the
results. Windows execution remains a stub and is outside these Linux harnesses.

## Clean repository dependencies and hosted release acceptance

For clean target checks, use `prepare_repository_dependencies.py` instead of the
local host-library repack builder. This opt-in preparation phase downloads signed
OS repository packages into a disposable standard Ubuntu 22.04 or Debian 12
container. It never executes QAVector or package maintainer scripts and verifies
that the base dpkg database did not change. No host HOME, secrets or existing
volumes are mounted. Only public dependency acquisition has networking:

```sh
.venv/bin/python packaging/prepare_repository_dependencies.py \
  --image sha256:REVIEWED_BASE_ID --allow-repository-downloads \
  --candidate build/runtime-release-candidate/qavector_0.16.29_amd64.deb \
  --previous installers/linux/0.16.28/qavector_0.16.28_amd64.deb \
  --output build/qa-repository-dependencies
.venv/bin/python packaging/accept_runtime_install.py \
  --image sha256:REVIEWED_BASE_ID --dependencies build/qa-repository-dependencies \
  --candidate build/runtime-release-candidate/qavector_0.16.29_amd64.deb \
  --previous installers/linux/0.16.28/qavector_0.16.28_amd64.deb \
  --output build/qa-clean-install
```

The install phase still uses `--network=none --pull=never`. Download provenance
contains the immutable image ID, exact packages and SHA256 values, repository
sources and signed InRelease metadata. The installer checks those archive hashes
before use; a failed preparation or modified dependency blocks execution. Qt
startup/shutdown is bounded with coreutils timeout and shell builtin signals,
so it does not depend on Ubuntu's external `/bin/kill` utility.

`build_runtime_preview.py` now stamps the version from `pyproject.toml` and creates
`BUILD-INFO.json` automatically. It pins application source files and complete
core/GUI payload inventories, rejects source changes during freeze and publishes
from staging on the output filesystem. The offline DEB packager verifies the
new payload inventories, including native library bytes/modes/symlinks. Existing
reviewed previews retain their earlier provenance format.

The manual workflow
[runtime-release-acceptance.yml](../.github/workflows/runtime-release-acceptance.yml)
builds on a disposable hosted Ubuntu 22.04 runner, requires genuine previous
DEB checksum verification, freezes the current source, runs native/frozen
and resource acceptance, and exercises both clean package targets. GUI permissions,
Service/Cycle Edit and quick controls run against the newly frozen core; profile
path checks ensure an explicit `CMS_HOME` agrees between source GUI and CLI.
GUI tests use private HOME/XDG/CMS_HOME directories and synthetic data. The Help →
About offline license viewer has its own GUI gate. Actions are pinned to full
commit IDs, checkout credentials are not persisted, and only the CI artifact
download step receives a token with `contents: read` and `actions: read`.
Diagnostic artifacts are
retained on failures; the candidate is stored only after all gates succeed. It
does not publish a GitHub release or use production/self-hosted runners.

Each GUI module runs in a separate pytest process to isolate Qt's global state.
A long mixed GUI regression process crashed while entering the Cycle Save event
loop; the complete Cycle page module and Save responsiveness checks passed in
fresh processes. The initial crash is retained in local diagnostics. Both GUI
workflows stop at the first failing module, and the release workflow stores a
separate JUnit report for each required module.

To activate hosted evidence, the complete runtime implementation and workflow
must exist in the repository used by Actions. Dispatch takes `previous_run_id`,
`previous_artifact_name` (default `qavector-verified-linux-deb`) and
`previous_deb_sha256`. The previous DEB is retrieved with
[`gh run download`](https://cli.github.com/manual/gh_run_download) from an
artifact of the specified Actions run **in the same repository**. Run IDs must
be positive integers. The artifact must contain exactly one previous amd64 DEB;
its independently reviewed SHA256 is checked before package processing.
GitHub Releases, release tags and public release discovery are not required.

The genuine local 0.16.28 artifact has SHA256
`ef16616c119f56c76dfd2b19529598f164e43deb3bd910bca464737a78b48610`;
the binary is intentionally ignored by Git. A previous CI build or a fixture
preparation job can supply that file as an artifact. If no earlier artifact is
available, one must be supplied for the upgrade test; publishing a release is
not part of that setup. Local acceptance continues to accept the existing DEB
directly through `accept_runtime_install.py --previous /path/to/previous.deb`.
The harness never relabels a current package to fabricate an upgrade. Local
clean-container passes and YAML validation are distinct from actual hosted CI
run evidence.
