"""Test-only curation of reviewed local tools; no binaries enter application packages."""

from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import zipfile

from execution_runtime.backends.linux import RuntimeImage
from execution_runtime.workers import GIT_CONTRACT, git_artifact


def git_image(directory, base_image):
    directory.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).resolve()
    executable = Path(shutil.which("git")).resolve()
    stdlib = Path(sysconfig.get_path("stdlib"))
    zipped = directory / "stdlib.zip"
    excluded = {"site-packages", "dist-packages", "test", "tests", "tkinter", "idlelib", "ensurepip", "turtledemo"}
    with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stdlib.rglob("*.py")):
            relative = path.relative_to(stdlib)
            if not excluded.intersection(relative.parts):
                archive.write(path, relative.as_posix())
    zip_target = stdlib.parent / ("python%d%d.zip" % sys.version_info[:2])
    targets = {one.target: (one.source, one.target, one.executable) for one in base_image.files}
    targets[str(python)] = (str(python), str(python), True)
    targets[str(executable)] = (str(executable), str(executable), True)
    targets[str(zip_target)] = (str(zipped), str(zip_target), False)
    landmark = stdlib / "os.py"
    if landmark.is_file():
        targets[str(landmark)] = (str(landmark), str(landmark), False)
    # Git reads local refs through upload-pack before copying objects. Its
    # subprocess shell and helper are also explicitly pinned; no host fallback.
    busybox = next(one for one in base_image.files if one.target == "/bin/busybox")
    targets["/bin/sh"] = (busybox.source, "/bin/sh", True)
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    exec_path = Path(subprocess.run([str(executable), "--exec-path"], check=True, capture_output=True,
                                   text=True, env=git_env).stdout.strip())
    upload_pack = exec_path / "git-upload-pack"
    targets[str(upload_pack)] = (str(upload_pack), str(upload_pack), True)
    extensions = Path(sysconfig.get_config_var("DESTSHARED"))
    needed = ("_posixsubprocess", "_ctypes", "_struct", "select", "math", "_random", "_sha512", "zlib", "fcntl",
              "_opcode", "_json", "binascii", "_sha2", "_sha1", "_blake2", "_md5", "_sha256", "_sha3", "grp", "pwd", "_datetime", "_uuid", "_heapq", "_bisect")
    native = [python, executable, upload_pack]
    for name in needed:
        for path in extensions.glob(name + ".*.so"):
            targets[str(path)] = (str(path), str(path), False)
            native.append(path)
    # ldd is used only on these explicitly selected, locally installed test tools.
    for path in native:
        inspected = subprocess.run(["ldd", str(path)], capture_output=True, text=True)
        for line in inspected.stdout.splitlines():
            match = re.search(r"(?:=>\s+|^\s*)(/\S+)", line)
            if match and Path(match.group(1)).is_file():
                target = match.group(1)
                targets[target] = (target, target, os.access(target, os.X_OK))
    artifact, _ = git_artifact("linux")
    entrypoint = "/qavector/workers/git_checkout_linux.py"
    targets[entrypoint] = (str(artifact), entrypoint, False)
    image = RuntimeImage.from_files(list(targets.values()))
    worker = dict(contract=GIT_CONTRACT, entrypoint=entrypoint,
                  argv=[str(python), "-I", "-B", entrypoint], git=str(executable))
    return image, worker


def review_image(base_image, interpreter):
    """Add the core wrapper and an explicitly named local provider fixture."""
    from execution_runtime.workers import AGENT_CONTRACT, agent_artifact
    source, _ = agent_artifact("linux")
    provider = Path(__file__).parent / "fixtures/runtime/review_provider.py"
    entrypoint, provider_target = "/qavector/workers/agent_review_linux.py", "/qavector/fixtures/review_provider.py"
    image = RuntimeImage.from_files([(one.source, one.target, one.executable) for one in base_image.files] +
                                   [(source, entrypoint, False), (provider, provider_target, False)])
    worker = dict(contract=AGENT_CONTRACT, entrypoint=entrypoint, argv=[interpreter, "-I", "-B", entrypoint],
                  provider_argv=[interpreter, "-I", "-B", provider_target], provider_entrypoint=provider_target)
    return image, worker
