# passt / pasta — third-party notice

Component: passt 2026_10_02.cba3570 — its `pasta` and `pesto` programs

Official project: https://passt.top/passt/

Upstream commit: `cba357068dc586e7540971eb40bb56c5e62c4de1`

Copyright Red Hat GmbH and the other passt contributors. The source files name
Stefano Brivio, David Gibson and Laurent Vivier as authors; the complete source
retains all original copyright, author and license notices. QAVector is a
distributor of this component, not its author or owner.

## License

passt, including the `pasta` and `pesto` programs QAVector distributes, is
licensed under the **GNU General Public License, version 2 or (at your option)
any later version (GPL-2.0-or-later)**. Two source files (`checksum.c` and
`virtio.c`) are additionally under **BSD-3-Clause** (`GPL-2.0-or-later AND
BSD-3-Clause`). The complete, verbatim license texts are in the adjacent
`GPL-2.0-or-later.txt` and `BSD-3-Clause.txt`, as shipped in the upstream
`LICENSES` directory. passt is provided without warranty as described in those
licenses.

## Corresponding source and changes

The Linux distribution includes the complete, unmodified upstream archive at
`runtime/sources/passt-2026_10_02.cba3570.tar.xz`, SHA-256:
`aa75616cc43925f0bd6c05bcfbbaecd77397c03d7bd23591b34ef535be5dfcdc`.
It also includes `build_bubblewrap.py` (which builds both runtime components),
`check_licenses.py` and the source lock `passt.lock.json` in the same sources
directory: the scripts used to control compilation and installation of the
executables, which allow an independent rebuild without QAVector's own source.
A C compiler and make are build prerequisites.

The accompanying QAVector build scripts and configuration are Copyright (C)
2026 Yurii Potapov and may be copied, modified and redistributed under
LGPL-2.1-or-later (see ../bubblewrap/COPYING), which permits their use under
the GPL as well. This permission applies to these corresponding build
materials, independently of the license chosen for the QAVector application,
and does not restrict passt recipients' rights to modify, rebuild or
redistribute it.

QAVector applies **no patches to passt source code**. It builds the `passt`,
`passt.avx2` and `pesto` make targets of the upstream Makefile; `pasta` and
`pasta.avx2` are the `passt` and `passt.avx2` binaries under those names,
exactly as upstream installs them on x86_64. The
target and versions are recorded in the lock and build manifest. A future
modified release must include the modified corresponding source and prominent,
dated change notices; it must not reuse this unmodified source declaration.

The upstream source is also available at
https://passt.top/passt/snapshot/passt-2026_10_02.cba3570.tar.xz
but an internet connection is not needed to read these notices or obtain the
source included with the Linux runtime distribution.

## Separate works

`pasta` and `pesto` remain separate executables, started by QAVector through a
process boundary and controlled through their documented command line and
control socket. Their code is not incorporated into QAVector's GUI or core,
and these terms do not relicense independent QAVector source.
