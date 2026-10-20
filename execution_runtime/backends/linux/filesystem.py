"""Pinned source objects and private, hash-verified runtime image copies."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from dataclasses import dataclass

from ..._json import digest
from ...errors import RuntimePolicyError
from .secrets import SECRET_DIRECTORY


def open_pinned(path, directory=False):
    """Open every path component without following symlinks, including parents."""
    path = PurePosixPath(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimePolicyError("unsafe_source", "Source must be an absolute path without traversal.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:]):
            flags = os.O_NOFOLLOW | os.O_CLOEXEC
            if index < len(path.parts) - 2 or directory:
                flags |= os.O_DIRECTORY | os.O_RDONLY
            else:
                flags |= os.O_PATH
            new = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = new
        info = os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimePolicyError("unsafe_source", "Only regular files and directories can be mounted.")
        return fd
    except BaseException:
        os.close(fd)
        raise


def target_path(value):
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or "\0" in value or "\\" in value or any(one in (".", "..", "") for one in value.split("/")[1:])):
        raise RuntimePolicyError("unsafe_target", "Expected an unambiguous absolute Linux target.")
    return value


def overlaps(first, second):
    a, b = PurePosixPath(first), PurePosixPath(second)
    return a == b or a in b.parents or b in a.parents


def reject_nested_mounts(source):
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            target = line.split()[4]
            for escaped, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
                target = target.replace(escaped, char)
            if source != target and PurePosixPath(source) in PurePosixPath(target).parents:
                raise RuntimePolicyError("nested_mount", "A workspace grant contains a nested host mount.")


@dataclass(frozen=True)
class RuntimeFile:
    source: str
    target: str
    sha256: str
    executable: bool


@dataclass(frozen=True)
class RuntimeImage:
    files: tuple

    def __post_init__(self):
        object.__setattr__(self, "files", tuple(self.files))
        if not self.files or len(self.files) > 4096:
            raise RuntimePolicyError("invalid_image", "Runtime image needs 1..4096 explicit regular files.")
        targets = []
        for entry in self.files:
            if (not isinstance(entry, RuntimeFile) or type(entry.executable) is not bool
                    or not isinstance(entry.source, str) or not PurePosixPath(entry.source).is_absolute()
                    or not isinstance(entry.sha256, str)):
                raise RuntimePolicyError("invalid_image", "Expected immutable runtime file descriptions.")
            target_path(entry.target)
            if (any(overlaps(entry.target, one) for one in targets + ["/proc", "/dev", "/tmp", SECRET_DIRECTORY])
                    or len(entry.sha256) != 64 or any(c not in "0123456789abcdef" for c in entry.sha256)):
                raise RuntimePolicyError("invalid_image", "Invalid image digest, reserved or overlapping target.")
            targets.append(entry.target)

    @classmethod
    def from_files(cls, entries):
        """Trusted provisioning helper, never applied to a workload-controlled list."""
        files = []
        for source, target, executable in entries:
            # Distribution library symlinks are resolved only during provisioning.
            source = os.path.realpath(source)
            fd = open_pinned(source)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RuntimePolicyError("invalid_image", "Runtime dependencies must be regular files.")
                with open("/proc/self/fd/%d" % fd, "rb") as handle:
                    hashed = hashlib.sha256()
                    for block in iter(lambda: handle.read(65536), b""):
                        hashed.update(block)
                files.append(RuntimeFile(source, target, hashed.hexdigest(), executable))
            finally:
                os.close(fd)
        return cls(tuple(files))

    @property
    def identity(self):
        return "sha256:" + digest({"version": 1, "files": [
            {"target": one.target, "sha256": one.sha256, "executable": one.executable}
            for one in sorted(self.files, key=lambda one: one.target)]})

    def materialize(self, root):
        root = Path(root)
        root.mkdir(mode=0o700)
        total = 0
        for entry in self.files:
            fd = open_pinned(entry.source)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RuntimePolicyError("invalid_image", "Runtime dependencies must be regular files.")
                destination = root / entry.target.lstrip("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                hashed = hashlib.sha256()
                with open("/proc/self/fd/%d" % fd, "rb") as source, open(destination, "xb") as output:
                    for block in iter(lambda: source.read(65536), b""):
                        total += len(block)
                        if total > 1024 ** 3:
                            raise RuntimePolicyError("image_too_large", "Runtime image exceeds 1 GiB.")
                        hashed.update(block)
                        output.write(block)
                if hashed.hexdigest() != entry.sha256:
                    raise RuntimePolicyError("image_changed", "Runtime dependency changed since approval.")
                destination.chmod(0o555 if entry.executable else 0o444)
            finally:
                os.close(fd)
        for name in ("proc", "dev", "tmp"):
            (root / name).mkdir(exist_ok=True)
