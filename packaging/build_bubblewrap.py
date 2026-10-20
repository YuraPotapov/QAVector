#!/usr/bin/env python3
# Copyright (C) 2026 Yurii Potapov
# SPDX-License-Identifier: LGPL-2.1-or-later
# License text: licenses/bubblewrap/COPYING (../licenses/bubblewrap/COPYING in the source bundle).
"""Build ER-BB-001 native resources from the reviewed source locks, never host tools.

Bubblewrap with its libcap, and passt's pasta and pesto (network isolation), each
from its pinned, checksummed upstream source, with licenses and that source.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
from check_licenses import passt_lock, source_lock, verify_licenses, verify_runtime


def verify_bundle(directory):
    """Stdlib-only rebuild/verification: no dependency on QAVector source code."""
    pin = source_lock()
    files = json.loads((directory / "manifests/checksums.json").read_text(encoding="utf-8"))["files"]
    for name, expected in files.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or checksum(directory / name) != expected:
            raise ValueError("Bundled runtime checksum mismatch: " + name)
    verify_runtime(directory)
    passt = passt_lock()
    for relative in ("bin/bwrap", "lib/libcap.so.2") + tuple(passt["executables"]):
        verify_elf(directory / relative, pin["target"]["minimum_glibc"])
    if run([directory / "bin/bwrap", "--version"], env={"LANG": "C"}) != "bubblewrap " + pin["version"]:
        raise ValueError("Built executable differs from the source pin")
    said = run([directory / "bin/pasta", "--version"], env={"LANG": "C"}).splitlines()[0]
    if said != "pasta " + passt["version"]:
        raise ValueError("Built pasta differs from the source pin: " + said)
    return {"source": "bundled", "path": str(directory / "bin/bwrap"),
            "version": pin["version"], "sha256": files["bin/bwrap"], "integrity": "verified"}


def run(arguments, **kwargs):
    return subprocess.run([str(one) for one in arguments], check=True, text=True,
                          capture_output=True, **kwargs).stdout.strip()


def checksum(path):
    hashed = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            hashed.update(block)
    return hashed.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o644)


def extract(archive, destination, skip_links=False):
    """``skip_links``: leave out symbolic links - passt's are between its test
    folders, which the build does not use."""
    with tarfile.open(archive) as source:
        for entry in source.getmembers():
            parts = Path(entry.name)
            if parts.is_absolute() or ".." in parts.parts or not (entry.isdir() or entry.isfile() or entry.issym()):
                raise ValueError("Unsafe upstream archive entry")
            if entry.issym() and skip_links:
                if Path(entry.linkname).is_absolute() or ".." in Path(entry.linkname).parts:
                    raise ValueError("Unsafe upstream archive link")
                continue
            if entry.issym():
                target = Path(entry.linkname)
                if target.is_absolute() or ".." in target.parts or not source.getmember(str(parts.parent / target)).isfile():
                    raise ValueError("Archive links must reference a regular file inside the source tree")
        source.extractall(destination, members=[entry for entry in source.getmembers() if not entry.issym()])
        for entry in source.getmembers():
            if entry.issym() and not skip_links:
                target = destination / Path(entry.name).parent / entry.linkname
                shutil.copyfile(target, destination / entry.name)


def verify_elf(path, baseline):
    header = path.read_bytes()[:20]
    if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise ValueError("Runtime target is Linux x86_64 ELF")
    versions = re.findall(r"GLIBC_([0-9]+\.[0-9]+)", run(["readelf", "--version-info", path]))
    maximum = max((tuple(map(int, one.split("."))) for one in versions), default=(0, 0))
    if maximum > tuple(map(int, baseline.split("."))):
        raise ValueError("Built ELF exceeds the declared glibc baseline")
    needed = set(re.findall(r"\(NEEDED\).*\[([^]]+)\]", run(["readelf", "-d", path])))
    if not needed <= {"libcap.so.2", "libc.so.6", "ld-linux-x86-64.so.2"}:
        raise ValueError("Uninventoried dynamic dependencies: " + ", ".join(sorted(needed)))


def build_passt(work, stage, archive=None):
    """pasta and pesto from the pinned passt source, into ``stage``; the archive used."""
    pin = passt_lock()
    if archive is None:
        archive = work / ("passt-%s.tar.xz" % pin["version"])
        with urllib.request.urlopen(pin["source_url"], timeout=30) as response, archive.open("xb") as destination:
            shutil.copyfileobj(response, destination)
    if checksum(archive) != pin["source_sha256"]:
        raise ValueError("passt source archive checksum differs from reviewed upstream pin")
    extract(archive, work / "passt-source", skip_links=True)
    tree = work / "passt-source" / ("passt-" + pin["version"])
    environment = dict(os.environ)
    environment.update({"CC": "cc", "SOURCE_DATE_EPOCH": str(pin["source_date_epoch"]), "LC_ALL": "C", "TZ": "UTC",
                        "CFLAGS": "-ffile-prefix-map=%s=/usr/src/qavector-runtime" % work,
                        "LDFLAGS": "-Wl,-z,relro,-z,now"})
    # Upstream's own flags (PIE, stack protector, fortify, its seccomp
    # filter). On x86_64 upstream installs an AVX2 variant beside pasta, which
    # pasta runs itself on a CPU that has AVX2 and says so when it is missing.
    run(["make", "-C", tree, "VERSION=" + pin["version"]] + pin["make_targets"], env=environment)
    for target, made in pin["executables"].items():
        shutil.copyfile(tree / made, stage / target)
        (stage / target).chmod(0o755)
        verify_elf(stage / target, pin["target"]["minimum_glibc"])
    return archive


def build(output, archive=None, libcap_license=None, libcap_library=None, passt_archive=None):
    pin = source_lock()
    license_root = ROOT / "licenses"
    verify_licenses(license_root, pin)
    if platform.system() != "Linux" or platform.machine() != pin["target"]["architecture"]:
        raise ValueError("Build this target natively on Linux x86_64")
    if output.exists():
        raise ValueError("Output already exists; use a fresh build staging directory")
    for command in ("meson", "ninja", "pkg-config", "cc", "make", "readelf"):
        if shutil.which(command) is None:
            raise ValueError("Build dependency missing: " + command)
    if libcap_license is None:
        libcap_license = next((path for path in (
            Path("/usr/share/doc/libcap2/copyright"), Path("/usr/share/licenses/libcap/LICENSE")) if path.is_file()), None)
    if libcap_license is None or not libcap_license.is_file():
        raise ValueError("Provide --libcap-license for the libcap version being bundled")
    libcap_version = run(["pkg-config", "--modversion", "libcap"])
    if libcap_library is None:
        libcap_library = Path(run(["pkg-config", "--variable=libdir", "libcap"])) / "libcap.so.2"
    libcap_library = libcap_library.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="qavector-bwrap-build-") as temporary:
        work = Path(temporary)
        if archive is None:
            archive = work / ("bubblewrap-" + pin["version"] + ".tar.xz")
            with urllib.request.urlopen(pin["source_url"], timeout=30) as response, archive.open("xb") as destination:
                shutil.copyfileobj(response, destination)
        if checksum(archive) != pin["source_sha256"]:
            raise ValueError("Source archive checksum differs from reviewed upstream pin")
        extract(archive, work / "source")
        source = work / "source" / ("bubblewrap-" + pin["version"])
        environment = dict(os.environ)
        environment.update({"CC": "cc", "SOURCE_DATE_EPOCH": str(pin["source_date_epoch"]), "LC_ALL": "C", "TZ": "UTC",
            "CFLAGS": "-O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2 -fPIE -ffile-prefix-map=%s=/usr/src/qavector-runtime" % work,
            "LDFLAGS": "-Wl,-z,relro,-z,now -pie"})
        run(["meson", "setup", work / "build", source] + pin["meson_options"], env=environment)
        run(["meson", "compile", "-C", work / "build"], env=environment)
        run(["meson", "install", "-C", work / "build", "--no-rebuild", "--destdir", work / "installed"], env=environment)
        stage = work / "bundle"
        for directory in ("bin", "lib", "licenses", "sources", "manifests"):
            (stage / directory).mkdir(parents=True)
        shutil.copyfile(work / "installed/bin/bwrap", stage / "bin/bwrap")
        (stage / "bin/bwrap").chmod(0o755)
        shutil.copyfile(libcap_library, stage / "lib/libcap.so.2")
        (stage / "lib/libcap.so.2").chmod(0o644)
        for name in ("bin/bwrap", "lib/libcap.so.2"):
            verify_elf(stage / name, pin["target"]["minimum_glibc"])
        passt = passt_lock()
        passt_archive = build_passt(work, stage, passt_archive)
        dynamic = run(["readelf", "-d", stage / "bin/bwrap"])
        if "[$ORIGIN/../lib]" not in dynamic:
            raise ValueError("Bundled launcher must use only its application-relative library RUNPATH")
        for name in ("COPYING", "LICENSE"):
            shutil.copyfile(source / name, stage / "licenses" / ("bubblewrap-" + name))
        shutil.copyfile(libcap_license, stage / "licenses/libcap-copyright")
        shutil.copytree(license_root / "bubblewrap", stage / "licenses/bubblewrap")
        shutil.copytree(license_root / "passt", stage / "licenses/passt")
        (stage / "licenses/libcap").mkdir()
        shutil.copyfile(libcap_license, stage / "licenses/libcap/COPYRIGHT")
        (stage / "licenses/libcap/NOTICE.md").write_text(
            "# libcap — third-party notice\n\nComponent: libcap %s\n\n" % libcap_version
            + "Copyright: Andrew G. Morgan and the other libcap contributors.\n"
            + "Official project: https://www.kernel.org/pub/linux/libs/security/linux-privs/libcap2/\n\n"
            + "The bundled libcap.so.2 is distributed under the BSD-3-Clause option of its\n"
            + "upstream dual BSD/GPL license. The adjacent COPYRIGHT preserves the supplied\n"
            + "copyright notices, full BSD conditions and disclaimer. QAVector does not claim\n"
            + "authorship or endorsement. The exact binary hash is in the runtime manifest.\n",
            encoding="utf-8")
        catalog = verify_licenses(license_root, pin)
        catalog["components"] = [entry for entry in catalog["components"] if entry["name"] in ("Bubblewrap", "passt")]
        catalog["components"].append({"name": "libcap", "version": libcap_version,
            "license": "BSD-3-Clause", "scope": "Private Linux Bubblewrap dependency.",
            "documents": ["libcap/NOTICE.md", "libcap/COPYRIGHT"]})
        write_json(stage / "licenses/components.json", catalog)
        source_name = "sources/bubblewrap-%s.tar.xz" % pin["version"]
        shutil.copyfile(archive, stage / source_name)
        shutil.copyfile(Path(__file__), stage / "sources/build_bubblewrap.py")
        shutil.copyfile(Path(__file__).with_name("check_licenses.py"), stage / "sources/check_licenses.py")
        shutil.copyfile(Path(__file__).with_name("runtime-build-requirements.txt"), stage / "sources/runtime-build-requirements.txt")
        write_json(stage / "sources/bubblewrap.lock.json", pin)
        shutil.copyfile(passt_archive, stage / ("sources/passt-%s.tar.xz" % passt["version"]))
        write_json(stage / "sources/passt.lock.json", passt)
        versions = {"schema_version": 1, "target": pin["target"], "linkage": pin["linkage"],
            "bubblewrap": {key: pin[key] for key in ("version", "upstream_commit", "source_url", "source_sha256")},
            "libcap": {"version": libcap_version, "license": "BSD-3-Clause", "bundled": True},
            "passt": dict({key: passt[key] for key in ("version", "upstream_commit", "source_url", "source_sha256",
                                                       "license", "source_licenses")},
                          executables=sorted(passt["executables"])),
            "build": {"source_date_epoch": pin["source_date_epoch"], "meson": run(["meson", "--version"]),
                      "compiler": run(["cc", "--version"]).splitlines()[0], "options": pin["meson_options"]}}
        write_json(stage / "manifests/versions.json", versions)
        components = []
        for name, version, license_name, relative in (
                ("bubblewrap", pin["version"], pin["license"], "bin/bwrap"),
                ("libcap", libcap_version, "BSD-3-Clause", "lib/libcap.so.2")):
            components.append({"type": "application" if name == "bubblewrap" else "library",
                "name": name, "version": version, "purl": "pkg:generic/%s@%s" % (name, version),
                "licenses": [{"expression": license_name}],
                "hashes": [{"alg": "SHA-256", "content": checksum(stage / relative)}]})
        components[0]["externalReferences"] = [{"type": "distribution", "url": pin["source_url"],
            "hashes": [{"alg": "SHA-256", "content": pin["source_sha256"]}]}]
        for relative in sorted(passt["executables"]):
            components.append({"type": "application", "name": Path(relative).name, "version": passt["version"],
                "purl": "pkg:generic/passt@%s" % passt["version"],
                "licenses": [{"expression": "GPL-2.0-or-later AND BSD-3-Clause"}],
                "hashes": [{"alg": "SHA-256", "content": checksum(stage / relative)}],
                "externalReferences": [{"type": "distribution", "url": passt["source_url"],
                    "hashes": [{"alg": "SHA-256", "content": passt["source_sha256"]}]}]})
        write_json(stage / "manifests/sbom.cdx.json", {"bomFormat": "CycloneDX", "specVersion": "1.6",
            "version": 1, "components": components})
        for path in stage.rglob("*"):
            if path.is_file() and path.parent != stage / "bin":
                path.chmod(0o644)
            elif path.is_dir():
                path.chmod(0o755)
        stage.chmod(0o755)
        write_json(stage / "manifests/checksums.json", {"schema_version": 1,
            "files": {str(path.relative_to(stage)): checksum(path) for path in sorted(stage.rglob("*")) if path.is_file()}})
        metadata = verify_bundle(stage)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output)
        print(json.dumps(dict(metadata, path=str(output / "bin/bwrap")), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/runtime")
    parser.add_argument("--archive", type=Path, help="Offline archive, still checked against source lock")
    parser.add_argument("--passt-archive", type=Path, help="Offline passt archive, still checked against its lock")
    parser.add_argument("--libcap-license", type=Path)
    parser.add_argument("--libcap-library", type=Path)
    parser.add_argument("--verify", type=Path, help="Check an already built runtime without compiling")
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify_bundle(args.verify), indent=2))
    else:
        build(args.output, args.archive, args.libcap_license, args.libcap_library, args.passt_archive)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout or "", error.stderr or "", file=sys.stderr)
        raise SystemExit(str(error))
