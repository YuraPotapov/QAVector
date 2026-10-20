"""Validate a previous DEB from a CI artifact or local fixture directory."""

import argparse
import hashlib
from pathlib import Path
import re
import subprocess


def verify_previous(directory, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("An independently reviewed SHA256 is required")
    archives = sorted(directory.glob("*.deb"))
    if len(archives) != 1:
        raise ValueError("Expected exactly one genuine previous amd64 DEB")
    archive = archives[0]
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError("Previous release checksum does not match the reviewed artifact")
    metadata = subprocess.run(["dpkg-deb", "--show", "--showformat=${Package} ${Architecture}", str(archive)],
        check=True, capture_output=True, text=True).stdout
    if metadata != "qavector amd64":
        raise ValueError("Expected a QAVector amd64 package")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("sha256")
    args = parser.parse_args()
    print(verify_previous(args.directory, args.sha256))
