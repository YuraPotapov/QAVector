# Bubblewrap — third-party notice

Component: Bubblewrap 0.13.1

Official repository: https://github.com/containers/bubblewrap

Upstream commit: `362936aea0d5c6e70f7af75d2d44b5a914cb4ba2`

Copyright (C) 2016 Alexander Larsson and the other Bubblewrap contributors.
The accompanying AUTHORS file preserves copyright notices from the source;
the complete source retains all original attribution and license notices.
QAVector is a distributor of this component, not its author or owner.

## License

The main upstream source headers identify **LGPL-2.0-or-later**. The compiled
`chroot_realpath.c` and `safe_openat.c` instead require **LGPL-2.1-or-later**.
Their notices credit Rick Sladkey, Dan Howell and Giuseppe Scrivano; the latter
file originates in crun. The complete executable is therefore distributed under
**LGPL-2.1-or-later**. The unmodified upstream COPYING (also named LICENSE and
COPYING.LIB in the release) contains the GNU Lesser General Public License,
version 2.1, February 1999. QAVector distributes this component under LGPL 2.1
or any later version.
The complete, verbatim license text is in the adjacent COPYING file. Original
notices have not been removed or replaced. Bubblewrap is provided without
warranty as described in that license.

## Corresponding source and changes

The Linux distribution includes the complete, unmodified upstream archive at
`runtime/sources/bubblewrap-0.13.1.tar.xz`, SHA-256:
`0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d`.
It also includes `build_bubblewrap.py`, `check_licenses.py`, the source lock and
build-tool requirements in the same sources directory. These describe the build
configuration and allow an independent rebuild without QAVector's own source.
Build tools and libcap development files are build prerequisites.

The accompanying QAVector build scripts and configuration are Copyright (C)
2026 Yurii Potapov and may be copied, modified and redistributed under
LGPL-2.1-or-later, whose complete text is provided in COPYING. This permission
applies to these corresponding build materials, independently of the license
chosen for the QAVector application. Their use does not restrict Bubblewrap
recipients' modification, rebuilding or redistribution rights.

QAVector applies **no patches to Bubblewrap source code**. Its compiler flags,
Meson options and target requirements are recorded in the lock and build
manifest. A future modified release must include the modified corresponding
source and prominent, dated change notices; it must not reuse this unmodified
source declaration.

The upstream source download is also available at
https://github.com/containers/bubblewrap/releases/download/v0.13.1/bubblewrap-0.13.1.tar.xz
but an internet connection is not needed to read these notices or obtain the
source included with the Linux runtime distribution.

## Separate works and dependencies

Bubblewrap remains a separate executable invoked through a process boundary;
its code is not incorporated into QAVector's GUI or core. These third-party
terms apply to Bubblewrap and do not relicense independent QAVector source.
Any QAVector distribution terms must preserve recipients' rights in this
component, including modification and redistribution under its license.

The private libcap shared library has its own copyright and license notices in
`licenses/libcap/`. The runtime uses host glibc and libseccomp; it does not ship
those libraries as part of the Bubblewrap resource bundle. A distributor adding
libraries or changing the linkage must review their obligations before release.
