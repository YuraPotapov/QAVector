# ER-LIC-001: Bubblewrap License Attribution & Compliance

Priority: **Mandatory**. Component: QAVector Execution Runtime.
Scope: bundled Bubblewrap distribution.

## Objective

Attribute Bubblewrap correctly and meet its license obligations wherever it is
integrated into and distributed with QAVector.

## Implementation requirements

1. Identify the exact bundled version, check the license of that release and
   its source, and account for the LGPL and the licenses of any additional
   bundled libraries.
2. Add a separate `licenses/bubblewrap/NOTICE.md` with the name, version,
   authors, license and official repository. Include the full text of the
   applicable license. Make the corresponding source available, modifications
   included.
3. Add **Third-Party Licenses** to the GUI, e.g. **Help → About → Third-Party
   Licenses**, showing Bubblewrap and its license. It must work offline.
4. Include the license materials in every distribution - DEB, AppImage and any
   future format. The build pipeline checks they are present and blocks a
   release without them.

## Acceptance criteria

- The exact license of the bundled version is verified; authorship is credited.
- The full license text is included in the distribution.
- The LGPL corresponding-source requirements are met.
- The GUI has an offline Third-Party Licenses section.
- The build pipeline checks for the mandatory license materials.
- QAVector's own code stays under its independently chosen license, unless
  other license obligations require otherwise.

**Do not remove, hide or alter Bubblewrap's authorship and license
information.** Do not give the impression that QAVector is the author or owner
of a third-party component.

Official source: [containers/bubblewrap](https://github.com/containers/bubblewrap).
Implementation and limits: [bundled-bubblewrap.md](bundled-bubblewrap.md).
