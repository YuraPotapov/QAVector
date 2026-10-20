# Bundled Bubblewrap (ER-BB-001)

Requirement: [QAVector_Bundled_Bubblewrap_Requirement.md](QAVector_Bundled_Bubblewrap_Requirement.md).
License attribution gate: [ER-LIC-001](QAVector_Bubblewrap_License_Requirement.md).

Restrictions on a project folder ([restrictions.md](restrictions.md)) and the
strict command-line isolation both start programs through Bubblewrap. QAVector
ships its own build of it; nobody installs the `bubblewrap` package, Docker or
a compiler to use them.

## Which binary runs

The resolver takes `runtime/` from `runtime_paths.app_root()` only - in a frozen
core that is `core/_internal/runtime/`. `CMS_HOME`, the workspace and `PATH` do
not decide it. A missing or changed bundle stops the launch; a system binary is
never picked up instead. A source checkout points `QAVECTOR_RUNTIME_DIR` at a
built runtime (or copies one to `/runtime/`, which git ignores); the same
integrity checks apply.

`linux-system-isolated` (an operator-chosen system Bubblewrap) exists only for
explicitly configured strict isolation, with the same probes and guarantees.

## Source pin and target

The single [source lock](../execution_runtime/backends/linux/bubblewrap.lock.json):

- Bubblewrap `0.13.1`, commit `362936aea0d5c6e70f7af75d2d44b5a914cb4ba2`,
  source SHA-256 `0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d`.
- Target: Linux x86_64, glibc 2.35 or newer, kernel 5.3 or newer with the
  namespaces, seccomp and pidfd APIs it needs. Limits additionally need the
  user's service manager to control the cgroup (see restrictions.md).
- The host side uses libseccomp 2.5+, which the DEB declares.

The verified configuration is dynamic glibc with a **bundled libcap.so.2** and an
`$ORIGIN/../lib` RUNPATH. The builder checks the ELF architecture, `DT_NEEDED` and
the highest `GLIBC_*` symbol versions, and refuses a binary above the target
baseline. A fully static binary is not claimed. SELinux label setters are
disabled (`-Dselinux=disabled`); host SELinux/AppArmor policy still applies.
The bundle being present is not the same as isolation working: before a
restricted program starts, a probe sandbox checks namespaces, no-new-privs,
seccomp and dropped capabilities.

## Build

The build machine needs a C toolchain, pkg-config, libcap development files,
`readelf`, and the pinned Meson/Ninja from
[runtime-build-requirements.txt](../packaging/runtime-build-requirements.txt).

```bash
python3 -m pip install -r packaging/runtime-build-requirements.txt
python3 packaging/build_bubblewrap.py --output=build/runtime
python3 packaging/build_bubblewrap.py --verify=build/runtime
```

`--archive=FILE` uses an offline source copy (its SHA-256 is still checked). The
output must be a new staging directory. The builder never uses an installed
bwrap and installs nothing on the host.

```text
runtime/
  bin/bwrap
  lib/libcap.so.2
  manifests/{versions.json,checksums.json,sbom.cdx.json}
  licenses/{bubblewrap-COPYING,bubblewrap-LICENSE,libcap-copyright,components.json}
  licenses/bubblewrap/{NOTICE.md,AUTHORS,COPYING}
  licenses/libcap/{NOTICE.md,COPYRIGHT}
  sources/{bubblewrap-0.13.1.tar.xz,bubblewrap.lock.json,build_bubblewrap.py,
           check_licenses.py,runtime-build-requirements.txt}
```

`packaging/build_deb.sh` builds the runtime, then the frozen core, and copies the
runtime in after PyInstaller's analysis so ELF RUNPATHs and checksums stay intact.
`qavector --runtime-bundle-info` checks the bundle and its version without
starting a workload.

## Licenses (ER-LIC-001)

The binary as a whole is **LGPL-2.1-or-later** (main headers LGPL-2.0-or-later,
two helpers LGPL-2.1-or-later). The unchanged upstream COPYING, AUTHORS, a
NOTICE with version, commit and sources, and the corresponding source archive
with the build scripts ship in the same bundle. Bundled libcap is used under its
**BSD-3-Clause** option with its own NOTICE and copyright file.

**Help → About → Third-Party Licenses** shows these texts offline.
`packaging/check_licenses.py --licenses=licenses` is the portable source gate;
`--runtime=DIR --licenses=GUI_LICENSE_DIR` checks a built bundle. The native
builder, both PyInstaller specs, DEB staging and the Windows installer build run
the gate before producing a distribution.

## Upgrades

A security upgrade is an explicit review: a new release or commit and its
changelog, an updated source lock (version, full commit, checksum), builds for
each target, the negative tests, ABI/inventory/license checks, and a new
application release. The runtime never downloads updates or replaces the binary
of an installed application.
