#!/usr/bin/env python3
"""Package a reviewed Linux preview offline; never install or modify user data."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

from check_licenses import verify_licenses, verify_runtime

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024 * 1024),b""):
            value.update(block)
    return value.hexdigest()


def inventory(directory, excluded=()):
    directory = Path(directory).resolve()
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if relative in excluded or relative == "DEBIAN" or relative.startswith("DEBIAN/"):
            continue
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or directory not in path.resolve().parents:
                raise ValueError("Application symlinks must stay within the staged tree: " + relative)
            result[relative] = {"symlink":target}
        elif path.is_file():
            result[relative] = {"sha256":digest(path),"mode":path.stat().st_mode & 0o777}
    return result


def inspect_preview(preview):
    preview = Path(preview)
    info = json.loads((preview / "BUILD-INFO.json").read_text())
    for component in ("core","gui"):
        binary = preview / component / ("qavector-" + component)
        if not os.access(binary,os.X_OK) or digest(binary) != info["binaries_sha256"][component]:
            raise ValueError("Reviewed preview binary changed: " + component)
        if info.get("preview_format") == 1 and inventory(preview / component) != info.get("payload",{}).get(component):
            raise ValueError("Reviewed preview payload changed: " + component)
        verify_licenses(preview / component / "_internal/licenses",require_libcap=True)
    verify_runtime(preview / "core/_internal/runtime")
    pins = info.get("source_sha256",{})
    if not pins:
        raise ValueError("The preview has no reviewed source hashes")
    for name, expected in pins.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or digest(ROOT / name) != expected:
            raise ValueError("Source changed since preview build; prepare a new preview")
    return info


def verify_stage(stage):
    app = Path(stage) / "opt/qavector"
    verify_runtime(app / "core/_internal/runtime")
    for component in ("core","gui"):
        verify_licenses(app / component / "_internal/licenses",require_libcap=True)


def package(preview, output, icons):
    if sys.platform != "linux":
        raise ValueError("This packager targets Linux DEB; Windows runtime remains a stub")
    preview, output, icons = Path(preview).resolve(),Path(output).absolute(),Path(icons).resolve()
    if output.exists() or output == preview or preview in output.parents:
        raise ValueError("Choose a new output directory outside the reviewed preview")
    inspect_preview(preview)
    icon = icons / "icon-256.png"
    if not icon.is_file():
        raise ValueError("Provide reviewed application icons, including icon-256.png")
    version = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(),re.M).group(1)
    architecture = subprocess.run(["dpkg","--print-architecture"],capture_output=True,text=True,check=True).stdout.strip()
    if architecture != "amd64":
        raise ValueError("This reviewed bundled runtime targets Linux amd64")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qavector-deb-",dir=output.parent) as temporary:
        work = Path(temporary)
        stage, published = work / "stage",work / "candidate"
        app, docs = stage / "opt/qavector",stage / "usr/share/doc/qavector"
        for path in (app,docs,stage / "DEBIAN",stage / "usr/bin",stage / "usr/share/applications",published):
            path.mkdir(parents=True,exist_ok=True)
        for component in ("core","gui"):
            source = preview / component
            shutil.copytree(source,app / component,symlinks=True)
            if inventory(source) != inventory(app / component):
                raise ValueError("Staged application differs from the reviewed preview")
        (app / "VERSION").write_text(version + "\n")
        for source, target in (("wrapper-core.sh","usr/bin/qavector"),("wrapper-gui.sh","usr/bin/qavector-gui"),
                ("qavector-gui.desktop","usr/share/applications/qavector.desktop"),("copyright","usr/share/doc/qavector/copyright"),
                ("postinst","DEBIAN/postinst"),("postrm","DEBIAN/postrm")):
            path = stage / target
            shutil.copy2(ROOT / "packaging/linux" / source,path)
            path.chmod(0o755 if target.startswith("usr/bin/") or source in ("postinst","postrm") else 0o644)
        for size in (16,24,32,48,64,128,256):
            source = icons / ("icon-%d.png" % size)
            if source.is_file():
                target = stage / ("usr/share/icons/hicolor/%dx%d/apps/qavector.png" % (size,size))
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
        shutil.copy2(preview / "BUILD-INFO.json",docs / "BUILD-INFO.json")
        (docs / "changelog.gz").write_bytes(gzip.compress((ROOT / "CHANGELOG.md").read_bytes(),mtime=0))
        manifest_name = "usr/share/doc/qavector/PACKAGE-MANIFEST.json"
        manifest = inventory(stage)
        (stage / manifest_name).write_text(json.dumps(manifest,indent=2) + "\n")
        size_kib = (sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()) + 1023) // 1024
        control = (ROOT / "packaging/linux/control.in").read_text()
        for key, value in {"VERSION":version,"MAINTAINER":"Yurii Potapov <potapovyura@gmail.com>","INSTALLED_SIZE":str(size_kib)}.items():
            control = control.replace("@" + key + "@",value)
        control = re.sub(r"(?m)^Architecture: .*","Architecture: " + architecture,control)
        (stage / "DEBIAN/control").write_text(control)
        verify_stage(stage)  # Mandatory gate immediately before the archive is made.
        archive = published / ("qavector_%s_%s.deb" % (version,architecture))
        # Gzip is supported by older dpkg versions and bounds compression to a
        # single worker; do not depend on the newer --threads-max option.
        subprocess.run(["dpkg-deb","--build","--root-owner-group","-Zgzip","-z1",str(stage),str(archive)],check=True)
        extracted = work / "extracted"
        subprocess.run(["dpkg-deb","--extract",str(archive),str(extracted)],check=True)
        verify_stage(extracted)
        if inventory(extracted,excluded=(manifest_name,)) != manifest:
            raise ValueError("DEB contents differ from the staged package manifest")
        # Invoke the extracted binary directly: host /opt wrappers must not run.
        with tempfile.TemporaryDirectory(prefix="qavector-package-smoke-") as profile:
            env = dict(os.environ,CMS_HOME=profile,XDG_CONFIG_HOME=profile + "/config",XDG_DATA_HOME=profile + "/data")
            core = extracted / "opt/qavector/core/qavector-core"
            result = subprocess.run([str(core),"--runtime-bundle-info"],env=env,capture_output=True,text=True,check=True,timeout=30)
            if json.loads(result.stdout).get("ok") is not True:
                raise ValueError("Extracted core could not verify its bundled runtime")
        checksum = digest(archive)
        report = {"archive":archive.name,"sha256":checksum,"version":version,"architecture":architecture,
            "manifest_entries":len(manifest),"licenses":"verified before and after archive extraction", "payload":"byte/mode/symlink match",
            "extracted_core_smoke":"passed","system_installation":"not performed","maintainer_scripts":"not executed",
            "dependencies_downloaded":False,"provider_requests":"not_sent","credential_access":"not_read"}
        (published / "PACKAGE-INFO.json").write_text(json.dumps(report,indent=2) + "\n")
        (published / "SHA256SUMS").write_text(checksum + "  " + archive.name + "\n")
        if output.exists():
            raise ValueError("Output appeared while packaging; existing output is preserved")
        published.rename(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--icons",required=True,type=Path)
    options = parser.parse_args()
    print(json.dumps(package(options.preview,options.output,options.icons),indent=2))


if __name__ == "__main__":
    main()
