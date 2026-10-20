#!/usr/bin/env python3
# Copyright (C) 2026 Yurii Potapov
# SPDX-License-Identifier: LGPL-2.1-or-later
# License text: licenses/bubblewrap/COPYING (../licenses/bubblewrap/COPYING in the source bundle).
"""ER-LIC-001 release gate, also shipped with the corresponding native source."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile


def source_lock():
    here = Path(__file__).resolve().parent
    path = here.parent / "execution_runtime/backends/linux/bubblewrap.lock.json"
    if not path.is_file():
        path = here / "bubblewrap.lock.json"
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_licenses(directory, pin=None, require_libcap=False):
    directory = Path(directory)
    pin = pin or source_lock()
    catalog = json.loads((directory / "components.json").read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1 or not isinstance(catalog.get("components"), list):
        raise ValueError("Invalid third-party license catalog")
    entries = {entry["name"]: entry for entry in catalog["components"]}
    bubblewrap = entries.get("Bubblewrap", {})
    if bubblewrap.get("version") != pin["version"] or bubblewrap.get("license") != pin["license"]:
        raise ValueError("Bubblewrap license catalog differs from the reviewed version/license")
    required = {"bubblewrap/NOTICE.md", "bubblewrap/AUTHORS", "bubblewrap/COPYING"}
    if not required <= set(bubblewrap.get("documents", [])):
        raise ValueError("Bubblewrap NOTICE, authors and complete license are mandatory")
    if digest(directory / "bubblewrap/COPYING") != pin["license_file_sha256"]:
        raise ValueError("The complete upstream Bubblewrap license must remain verbatim")
    notice = (directory / "bubblewrap/NOTICE.md").read_text(encoding="utf-8")
    for expected in (pin["version"], pin["upstream_commit"], pin["source_sha256"], pin["license"],
                     "https://github.com/containers/bubblewrap", "Alexander Larsson", "LGPL 2.1"):
        if expected not in notice:
            raise ValueError("Bubblewrap attribution/source notice is incomplete: " + expected)
    for entry in catalog["components"]:
        if not entry.get("documents"):
            raise ValueError("Every component needs offline license documents")
        for name in entry["documents"]:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or str(path) != name or "\\" in name:
                raise ValueError("License document must be inside the distribution")
            material = directory / name
            if material.is_symlink() or not material.is_file() or not material.read_bytes().strip():
                raise ValueError("Required offline license material is missing or empty: " + name)
    if require_libcap:
        libcap = entries.get("libcap", {})
        if libcap.get("license") != "BSD-3-Clause" or not libcap.get("version"):
            raise ValueError("The bundled libcap needs its exact version and selected BSD license")
        if not {"libcap/NOTICE.md", "libcap/COPYRIGHT"} <= set(libcap.get("documents", [])):
            raise ValueError("Bundled libcap copyright and notice are mandatory")
        text = (directory / "libcap/COPYRIGHT").read_text(encoding="utf-8")
        for expected in ("Andrew G. Morgan", "Redistribution", "binary", "DISCLAIMED", "DAMAGE"):
            if expected not in text:
                raise ValueError("The libcap copyright file must contain the complete BSD license")
    return catalog


def verify_runtime(directory):
    directory = Path(directory)
    pin = source_lock()
    catalog = verify_licenses(directory / "licenses", pin, require_libcap=True)
    archive = directory / ("sources/bubblewrap-%s.tar.xz" % pin["version"])
    if digest(archive) != pin["source_sha256"]:
        raise ValueError("Complete, unmodified corresponding source must accompany the executable")
    with tarfile.open(archive) as source:
        prefix = "bubblewrap-%s/" % pin["version"]
        license_text = source.extractfile(prefix + "COPYING").read()
        if license_text != (directory / "licenses/bubblewrap/COPYING").read_bytes():
            raise ValueError("Distributed license differs from corresponding source")
        authors = (directory / "licenses/bubblewrap/AUTHORS").read_text(encoding="utf-8")
        for member in source.getmembers():
            if member.isfile() and member.name.endswith((".c", ".h")):
                data = source.extractfile(member).read().decode("utf-8")
                for line in data.splitlines():
                    if "Copyright" in line and line.strip().lstrip("*").strip() not in authors:
                        raise ValueError("An original Bubblewrap copyright notice is missing")
    for name in ("build_bubblewrap.py", "check_licenses.py", "bubblewrap.lock.json", "runtime-build-requirements.txt"):
        if not (directory / "sources" / name).is_file():
            raise ValueError("Corresponding build configuration is missing: " + name)
    if json.loads((directory / "sources/bubblewrap.lock.json").read_text(encoding="utf-8")) != pin:
        raise ValueError("Corresponding build source lock differs from the application")
    versions = json.loads((directory / "manifests/versions.json").read_text(encoding="utf-8"))
    libcap = next(entry for entry in catalog["components"] if entry["name"] == "libcap")
    if libcap["version"] != versions["libcap"]["version"]:
        raise ValueError("The libcap license catalog and binary manifest disagree")
    return catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--licenses", type=Path)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    if not (args.licenses or args.runtime):
        parser.error("provide --licenses and/or --runtime")
    if args.licenses:
        verify_licenses(args.licenses)
    if args.runtime:
        catalog = verify_runtime(args.runtime)
        if args.licenses:
            if verify_licenses(args.licenses, require_libcap=True) != catalog:
                raise ValueError("GUI and runtime third-party component versions must agree")
            for entry in catalog["components"]:
                for name in entry["documents"]:
                    if (args.licenses / name).read_bytes() != (args.runtime / "licenses" / name).read_bytes():
                        raise ValueError("The offline GUI must include the complete runtime notices: " + name)
    print("ER-LIC-001: required attribution, licenses and corresponding source verified")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        raise SystemExit("ER-LIC-001 release blocked: " + str(error))
