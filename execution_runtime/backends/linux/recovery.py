"""Private instance leases and conservative recovery; never signal stored PIDs."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import select
import stat

from ..._json import read_document
from ...errors import RuntimePolicyError
from ...registry import _atomic
from .cgroups import CgroupParent, Cgroup

INSTANCE_ID = re.compile(r"[0-9a-f]{32}\Z")


def process_identity(pid):
    fields = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "start_ticks": int(fields[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def identity_exited(identity):
    if (not isinstance(identity, dict) or set(identity) != {"pid", "start_ticks", "boot_id"}
            or type(identity["pid"]) is not int or identity["pid"] <= 0
            or type(identity["start_ticks"]) is not int or not isinstance(identity["boot_id"], str)):
        raise RuntimePolicyError("invalid_recovery_record", "Missing process identity evidence.")
    try:
        fd = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return True
    try:
        try:
            current = process_identity(identity["pid"])
        except FileNotFoundError:
            return True
        return current != identity or bool(select.select([fd], [], [], 0)[0])
    finally:
        os.close(fd)


def lock_file(path, create=False, nonblocking=False):
    import fcntl  # Linux only; importing an unconfigured provider stays portable.
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    fd = os.open(str(path), flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise RuntimePolicyError("unsafe_lease", "Instance leases must be private regular files.")
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def state_lock(directory):
    fd = lock_file(Path(directory) / "state.lock", create=True)
    try:
        yield
    finally:
        os.close(fd)


def new_instance(directory, identity, policy_digest, runtime_root_identity, cgroup_parent, provider="linux-isolated"):
    """Publish a leased preparation record under the store lock before any setup."""
    with state_lock(directory):
        path = Path(directory) / identity
        path.mkdir(mode=0o700)
        lease = None
        try:
            lease = lock_file(path / "instance.lock", create=True, nonblocking=True)
            record = {"kind": "ExecutionInstance", "schema_version": 1,
                "instance_id": identity, "provider": provider, "policy_digest": policy_digest,
                "runtime_root_identity": runtime_root_identity, "state": "preparing",
                "recovery": {"owner": process_identity(os.getpid()), "init": None,
                             "cgroup_parent": cgroup_parent}}
            _atomic(path / "runtime.json", record)
            return path, lease
        except BaseException:
            if lease is not None:
                os.close(lease)
            import shutil
            shutil.rmtree(path)
            raise


def recover_instance(path, cgroup_parent, provider="linux-isolated"):
    import shutil
    import time
    record_path = path / "runtime.json"
    if record_path.is_symlink():
        raise RuntimePolicyError("invalid_recovery_record", "Recovery records cannot be symlinks.")
    record = read_document(record_path)
    if (not isinstance(record, dict) or record.get("kind") != "ExecutionInstance"
            or type(record.get("schema_version")) is not int or record["schema_version"] != 1
            or record.get("instance_id") != path.name or record.get("provider") != provider
            or not isinstance(record.get("recovery"), dict)):
        raise RuntimePolicyError("invalid_recovery_record", "Unknown instance record; resources were retained.")
    if record.get("state") == "cleaned":
        return False
    evidence = record["recovery"]
    if not identity_exited(evidence.get("owner")):
        raise RuntimePolicyError("owner_still_running", "Instance owner has not exited.")
    phase = record.get("state")
    if phase not in ("preparing", "prepared", "exited", "failed", "starting", "running", "launching"):
        raise RuntimePolicyError("invalid_recovery_record", "Unknown recovery state.")
    failed_before_spawn = phase == "failed" and evidence.get("failed_before_spawn") is True
    if phase in ("launching", "starting", "running", "failed") and not failed_before_spawn:
        if evidence.get("init") is None:
            raise RuntimePolicyError("termination_unconfirmed", "Incomplete launch evidence; resources were retained.")
        if not identity_exited(evidence["init"]):
            raise RuntimePolicyError("descendants_still_running", "Namespace termination is not confirmed.")
    parent = evidence.get("cgroup_parent")
    if parent is not None:
        if cgroup_parent is None or parent != str(Path(cgroup_parent)):
            raise RuntimePolicyError("recovery_configuration_mismatch", "Recovery requires the original delegated cgroup parent.")
        CgroupParent(parent)
        Cgroup(Path(parent) / ("qavector-" + path.name)).cleanup()
    root = path / "root"
    if root.is_symlink():
        raise RuntimePolicyError("unsafe_recovery_root", "Recovery root cannot be a symlink.")
    if root.exists():
        shutil.rmtree(root)
    record["state"] = "cleaned"
    if phase in ("preparing", "prepared"):
        record["reason"] = "preparation_abandoned"
    elif phase in ("launching", "starting", "running"):
        record["reason"] = "supervisor_lost"
    record["recovered_at"] = time.time()
    _atomic(record_path, record)
    return True
