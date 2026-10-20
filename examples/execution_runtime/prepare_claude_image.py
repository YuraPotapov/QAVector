"""Prepare an explicit Linux Claude image; never read sign-in or call a provider.

Run with the reviewed Python interpreter to include. ldd inspects only the
explicitly selected reviewed ELF tools and Python extensions. No tool discovery,
account login, dependency download, registry write or Service/Cycle edit occurs.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import zipfile

from execution_runtime.workers import claude_artifact


def prepare(directory, claude, busybox):
    if sys.platform != "linux" or getattr(sys, "frozen", False):
        raise ValueError("Run this helper with the reviewed Linux Python interpreter; Windows remains a stub.")
    from execution_runtime.backends.linux import RuntimeImage
    directory = Path(directory).absolute()
    selected = [Path(sys.executable).resolve(strict=True), Path(claude).resolve(strict=True), Path(busybox).resolve(strict=True)]
    for path in selected:
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF" or not os.access(path, os.X_OK):
                raise ValueError("Select reviewed executable ELF tools.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    python, cli, shell = selected
    stdlib = Path(sysconfig.get_path("stdlib"))
    archive_path = directory / "stdlib.zip"
    excluded = {"site-packages", "dist-packages", "test", "tests", "tkinter", "idlelib", "ensurepip", "turtledemo", "__pycache__"}
    with zipfile.ZipFile(archive_path,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stdlib.rglob("*.py")):
            relative = path.relative_to(stdlib)
            if not excluded.intersection(relative.parts):
                archive.write(path,relative.as_posix())
    zip_target = stdlib.parent / ("python%d%d.zip" % sys.version_info[:2])
    targets = {str(python):(str(python),str(python),True), "/approved/claude":(str(cli),"/approved/claude",True),
        "/bin/busybox":(str(shell),"/bin/busybox",True), "/bin/sh":(str(shell),"/bin/sh",True),
        str(zip_target):(str(archive_path),str(zip_target),False)}
    landmark = stdlib / "os.py"
    if landmark.is_file():
        targets[str(landmark)] = (str(landmark),str(landmark),False)
    native = list(selected)
    extensions = Path(sysconfig.get_config_var("DESTSHARED"))
    for name in ("_posixsubprocess","_ctypes","_struct","select","math","_random","_sha512","zlib","fcntl", "_socket","_ssl","array",
            "_opcode","_json","binascii","_sha2","_sha1","_blake2","_md5","_sha256","_sha3","grp","pwd","_datetime","_uuid","_heapq","_bisect"):
        for path in extensions.glob(name + ".*.so"):
            targets[str(path)] = (str(path),str(path),False)
            native.append(path)
    for path in native:
        result = subprocess.run(["/usr/bin/ldd",str(path)],env={"PATH":"/usr/bin:/bin","LANG":"C"},
            capture_output=True,text=True,timeout=10)
        if "not found" in result.stdout:
            raise ValueError("A reviewed tool has unresolved dynamic dependencies.")
        if result.returncode and "not a dynamic executable" not in result.stderr + result.stdout and "statically linked" not in result.stdout:
            raise ValueError("Cannot inspect the reviewed tool's dynamic dependencies.")
        for line in result.stdout.splitlines():
            match = re.search(r"(?:=>\s+|^\s*)(/\S+)",line)
            if match and Path(match.group(1)).is_file():
                target = match.group(1)
                targets[target] = (target,target,os.access(target,os.X_OK))
    source, _ = claude_artifact("linux")
    entry = "/qavector/workers/claude_review_linux.py"
    targets[entry] = (str(source),entry,False)
    image = RuntimeImage.from_files(list(targets.values()))
    manifest = {"identity":image.identity,"files":[vars(one) for one in image.files]}
    (directory / "image.json").write_text(json.dumps(manifest,indent=2) + "\n")
    result = {"image":str(directory / "image.json"), "interpreter":str(python), "executable":"/approved/claude",
        "identity":image.identity, "provider_request":"not_sent", "credential_access":"not_read"}
    (directory / "PREPARATION.json").write_text(json.dumps(result,indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude",required=True,help="Explicit reviewed native Claude executable; no PATH lookup")
    parser.add_argument("--busybox",required=True,help="Explicit reviewed BusyBox required by native prerequisite probes")
    parser.add_argument("--output",required=True,help="New image directory; must not exist")
    options = parser.parse_args()
    print(json.dumps(prepare(options.output,options.claude,options.busybox),indent=2))


if __name__ == "__main__":
    main()
