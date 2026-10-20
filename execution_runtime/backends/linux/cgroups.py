"""Delegated cgroups v2 only. Never change a host service or parent delegation."""

import ctypes
import os
from pathlib import Path
import uuid

from ...errors import RuntimePolicyError
from .filesystem import open_pinned


def _write(path, value):
    with open(path, "w", encoding="ascii") as handle:
        handle.write(str(value))


def _pairs(path):
    try:
        return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}
    except OSError:
        return {}


class CgroupParent:
    def __init__(self, directory):
        self.directory = Path(directory)
        if not self.directory.is_absolute() or not str(self.directory).startswith("/sys/fs/cgroup/"):
            raise RuntimePolicyError("invalid_cgroup_parent", "Expected an explicitly delegated cgroup v2 directory.")
        fd = open_pinned(str(self.directory), directory=True)
        try:
            # fstatfs first field is Linux fs magic; buffer accommodates native ABI.
            data = ctypes.create_string_buffer(256)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.fstatfs(fd, ctypes.byref(data)) != 0 or ctypes.c_long.from_buffer(data).value != 0x63677270:
                raise RuntimePolicyError("invalid_cgroup_parent", "Resource controls require the real cgroup v2 filesystem.")
        finally:
            os.close(fd)

    def create(self, limits, instance_id=None):
        identity = instance_id or uuid.uuid4().hex
        if len(identity) != 32 or any(one not in "0123456789abcdef" for one in identity):
            raise RuntimePolicyError("invalid_instance_id", "Expected a native instance identity.")
        path = self.directory / ("qavector-" + identity)
        try:
            path.mkdir()
        except OSError:
            raise RuntimePolicyError("cgroup_delegation_unavailable", "Cannot create a child in the delegated cgroup.") from None
        group = Cgroup(path)
        try:
            for name, value in self.settings(limits).items():
                if not (path / name).is_file():
                    raise RuntimePolicyError("cgroup_controller_unavailable", "Required controller is not delegated/enabled.")
                _write(path / name, value)
                if (path / name).read_text().strip() != str(value):
                    raise RuntimePolicyError("cgroup_limit_mismatch", "Kernel did not apply the requested resource limit.")
            return group
        except BaseException:
            path.rmdir()
            raise

    @staticmethod
    def settings(limits):
        unsupported = set(limits) - {"cpu_cores", "memory", "tasks"}
        if unsupported:
            raise RuntimePolicyError("unsupported_limit", "Linux backend does not implement the requested resource semantics.")
        result = {}
        if "cpu_cores" in limits:
            if limits["cpu_cores"] > 2 ** 63 / 100000:
                raise RuntimePolicyError("unsupported_limit", "CPU quota exceeds the kernel counter range.")
            quota = int(limits["cpu_cores"] * 100000)
            if quota < 1000:
                raise RuntimePolicyError("unsupported_limit", "CPU quota is below the kernel scheduling granularity.")
            result["cpu.max"] = "%d 100000" % quota
        if "memory" in limits:
            if limits["memory"]["accounting"] != "charged":
                raise RuntimePolicyError("unsupported_accounting", "memory.max accounts charged cgroup memory, not RSS or committed address space.")
            size = limits["memory"]["bytes"]
            if size % os.sysconf("SC_PAGE_SIZE"):
                raise RuntimePolicyError("unsupported_limit", "Cgroup memory limit must be page-aligned.")
            result.update({"memory.max": str(size), "memory.swap.max": "0", "memory.oom.group": "1"})
        if "tasks" in limits:
            if limits["tasks"] < 4:
                raise RuntimePolicyError("unsupported_limit", "Task budget must include the monitor, init and workload (minimum 4).")
            result["pids.max"] = str(limits["tasks"])
        return result


class Cgroup:
    def __init__(self, path):
        self.path = Path(path)

    def metrics(self):
        result = {"cpu": _pairs(self.path / "cpu.stat"), "memory_events": _pairs(self.path / "memory.events"),
                  "pids_events": _pairs(self.path / "pids.events")}
        for name in ("memory.current", "memory.peak", "pids.current", "pids.peak"):
            try:
                result[name] = int((self.path / name).read_text().strip())
            except OSError:
                pass
        return result

    def kill(self):
        path = self.path / "cgroup.kill"
        if path.exists():
            _write(path, "1")

    def cleanup(self):
        if not self.path.exists():
            return
        if _pairs(self.path / "cgroup.events").get("populated", 0):
            raise RuntimePolicyError("cgroup_still_populated", "Cannot release a cgroup with live descendants.")
        self.path.rmdir()
