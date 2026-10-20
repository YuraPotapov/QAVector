"""Integrity-checked application resources; never search PATH or user data."""

import hashlib
from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import subprocess

from ...errors import RuntimePolicyError
from .filesystem import open_pinned


def source_lock():
    return json.loads(resources.read_text(__package__, "bubblewrap.lock.json", encoding="utf-8"))


def passt_lock():
    """The reviewed pin of passt, whose pasta and pesto ship beside bwrap."""
    return json.loads(resources.read_text(__package__, "passt.lock.json", encoding="utf-8"))


def _version_tuple(value):
    try:
        return tuple(int(part) for part in value.split(".")[:2])
    except (ValueError, AttributeError):
        return ()


class BundledLauncher:
    def __init__(self, directory=None):
        from runtime_paths import execution_runtime_root
        self.directory = Path(directory if directory is not None else execution_runtime_root()).absolute()
        self._verified_version = None

    def _read(self, relative):
        path = self.directory / relative
        for directory in (self.directory, path.parent):
            pinned = open_pinned(str(directory), directory=True)
            try:
                if os.fstat(pinned).st_mode & 0o022:
                    raise RuntimePolicyError("unsafe_bundled_runtime", "Bundled resource directories cannot be writable by other users.")
            finally:
                os.close(pinned)
        fd = open_pinned(str(path))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise RuntimePolicyError("unsafe_bundled_runtime", "Bundled resources must be regular files not writable by other users.")
            with open("/proc/self/fd/%d" % fd, "rb") as stream:
                data = stream.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise RuntimePolicyError("bundle_resource_too_large", "Runtime resource exceeds its integrity limit.")
            return data, info.st_mode
        finally:
            os.close(fd)

    def inspect(self, check_host=True):
        if os.name != "posix":
            raise RuntimePolicyError("bundle_target_unsupported", "The Linux runtime is unavailable on this OS.")
        try:
            versions = json.loads(self._read("manifests/versions.json")[0])
            checksums = json.loads(self._read("manifests/checksums.json")[0])
            expected = source_lock()
            if (not isinstance(versions, dict) or type(versions.get("schema_version")) is not int or versions.get("schema_version") != 1
                    or versions.get("bubblewrap") != {key: expected[key] for key in (
                        "version", "upstream_commit", "source_url", "source_sha256")}
                    or versions.get("target") != expected["target"]
                    or versions.get("linkage") != expected["linkage"]
                    or not isinstance(checksums, dict) or type(checksums.get("schema_version")) is not int or checksums.get("schema_version") != 1
                    or not isinstance(checksums.get("files"), dict)):
                raise RuntimePolicyError("bundle_manifest_mismatch", "Bundled runtime metadata does not match the reviewed source pin.")
            files = checksums["files"]
            required = {"bin/bwrap", "lib/libcap.so.2", "manifests/versions.json", "manifests/sbom.cdx.json",
                        "licenses/bubblewrap-COPYING", "licenses/bubblewrap-LICENSE", "licenses/libcap-copyright",
                        "licenses/components.json", "licenses/bubblewrap/NOTICE.md", "licenses/bubblewrap/AUTHORS",
                        "licenses/bubblewrap/COPYING", "licenses/libcap/NOTICE.md", "licenses/libcap/COPYRIGHT",
                        "sources/bubblewrap-%s.tar.xz" % expected["version"],
                        "sources/build_bubblewrap.py", "sources/check_licenses.py",
                        "sources/runtime-build-requirements.txt", "sources/bubblewrap.lock.json"}
            if not required <= set(files) or len(files) > 64:
                raise RuntimePolicyError("bundle_manifest_incomplete", "Runtime binary, libraries, source, inventory and licenses must all be shipped.")
            if files["sources/bubblewrap-%s.tar.xz" % expected["version"]] != expected["source_sha256"]:
                raise RuntimePolicyError("bundle_source_mismatch", "Corresponding source differs from the reviewed upstream archive.")
            if files["licenses/bubblewrap/COPYING"] != expected["license_file_sha256"]:
                raise RuntimePolicyError("bundle_license_mismatch", "The complete upstream license must accompany the runtime unchanged.")
            for name, checksum in files.items():
                parts = PurePosixPath(name)
                if (not isinstance(name, str) or parts.is_absolute() or ".." in parts.parts
                        or "\\" in name or "\0" in name or str(parts) != name
                        or not isinstance(checksum, str) or len(checksum) != 64):
                    raise RuntimePolicyError("bundle_manifest_invalid", "Invalid runtime integrity entry.")
                data, mode = self._read(name)
                if hashlib.sha256(data).hexdigest() != checksum:
                    raise RuntimePolicyError("bundle_integrity_failed", "A bundled runtime resource differs from its release checksum.")
                if name == "bin/bwrap" and (not mode & 0o111 or mode & 0o6000):
                    raise RuntimePolicyError("bundle_not_executable", "Bundled Bubblewrap must be executable without setuid/setgid.")
            if check_host:
                target = expected["target"]
                if platform.system() != "Linux" or platform.machine() != target["architecture"]:
                    raise RuntimePolicyError("bundle_target_unsupported", "The bundled runtime does not support this OS/architecture.")
                libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
                if not libc.startswith("glibc ") or _version_tuple(libc.split()[-1]) < _version_tuple(target["minimum_glibc"]):
                    raise RuntimePolicyError("bundle_abi_unsupported", "This runtime requires the documented glibc baseline.")
                if _version_tuple(platform.release().split("-")[0]) < _version_tuple(target["minimum_kernel"]):
                    raise RuntimePolicyError("bundle_kernel_unsupported", "This runtime requires the documented kernel baseline and successful native probes.")
            return {"source": "bundled", "path": str(self.directory / "bin/bwrap"),
                    "version": expected["version"], "sha256": files["bin/bwrap"],
                    "target": expected["target"], "linkage": expected["linkage"], "integrity": "verified"}
        except RuntimePolicyError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise RuntimePolicyError("bundled_runtime_unavailable", "QAVector's bundled runtime is missing or invalid; reinstall a complete supported application build.") from None

    def tool(self, name):
        """The bundled ``pasta`` or ``pesto`` (passt, GPL-2.0-or-later), integrity-checked
        with the rest of the runtime. A build without them cannot isolate a network."""
        if name not in ("pasta", "pesto"):
            raise RuntimePolicyError("bundle_tool_unknown", "Unknown runtime tool.")
        metadata = self.inspect()
        files = json.loads(self._read("manifests/checksums.json")[0])["files"]
        if "bin/" + name not in files:
            raise RuntimePolicyError("isolated_network_unavailable",
                "This QAVector build has no network isolation (pasta); install a build that includes it.")
        data, mode = self._read("bin/" + name)
        if not mode & 0o111 or mode & 0o6000:
            raise RuntimePolicyError("bundle_not_executable", "Bundled %s must be executable without setuid/setgid." % name)
        return {"path": str(self.directory / "bin" / name), "sha256": files["bin/" + name],
                "version": passt_lock()["version"], "bundle": metadata["path"]}

    def executable(self):
        metadata = self.inspect()
        if self._verified_version != metadata["sha256"]:
            try:
                result = subprocess.run([metadata["path"], "--version"], capture_output=True,
                    env={"LANG": "C"}, timeout=3, check=True)
            except (OSError, subprocess.SubprocessError):
                raise RuntimePolicyError("bundle_version_probe_failed", "Bundled Bubblewrap cannot run with this host ABI/security configuration.") from None
            if result.stdout.decode("utf-8", "replace").strip() != "bubblewrap " + metadata["version"]:
                raise RuntimePolicyError("bundle_version_mismatch", "Bundled executable version differs from release metadata.")
            self._verified_version = metadata["sha256"]
        return metadata
