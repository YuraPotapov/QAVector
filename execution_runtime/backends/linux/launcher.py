"""Explicit, content-pinned provisioning of an operator-approved host launcher."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile

from ...errors import RuntimePolicyError
from .filesystem import RuntimeImage, open_pinned


@dataclass(frozen=True)
class ApprovedLauncher:
    image: RuntimeImage

    def __post_init__(self):
        if (not isinstance(self.image, RuntimeImage) or len(self.image.files) != 1
                or self.image.files[0].target != "/bwrap" or not self.image.files[0].executable):
            raise RuntimePolicyError("invalid_launcher", "Expected one approved executable launcher.")

    @classmethod
    def from_file(cls, source):
        """Trusted operator approval, never performed on workload-controlled input."""
        return cls(RuntimeImage.from_files([(source, "/bwrap", True)]))

    @property
    def identity(self):
        return "sha256:" + self.image.files[0].sha256

    def materialize(self, state_directory):
        directory = Path(state_directory) / "launchers"
        directory.mkdir(mode=0o700, exist_ok=True)
        pinned = open_pinned(str(directory), directory=True)
        os.close(pinned)
        binary = directory / (self.image.files[0].sha256 + ".bwrap")
        if not binary.exists():
            with tempfile.TemporaryDirectory(prefix="provision-", dir=directory) as temporary:
                root = Path(temporary) / "root"
                self.image.materialize(root)
                os.replace(root / "bwrap", binary)
        fd = open_pinned(str(binary))
        try:
            with open("/proc/self/fd/%d" % fd, "rb") as handle:
                hashed = hashlib.sha256()
                for block in iter(lambda: handle.read(65536), b""):
                    hashed.update(block)
            if hashed.hexdigest() != self.image.files[0].sha256:
                raise RuntimePolicyError("launcher_changed", "Approved launcher copy changed since provisioning.")
        finally:
            os.close(fd)
        return str(binary)
