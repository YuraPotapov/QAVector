"""Native-only libseccomp BPF; filters are loaded by bwrap before workload exec."""

import ctypes
import ctypes.util
import errno
import tempfile

from ...errors import RuntimePolicyError

ALLOW = 0x7fff0000
ERRNO = 0x00050000


class _Compare(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def filter_file(loopback_tcp=False, restricted=False):
    """The syscall filter bwrap loads before the workload starts.

    ``restricted`` is the profile for programs held to their project folder:
    they run the host's own tools, so sockets stay available - whether they
    reach anything is decided by the network namespace (``--unshare-net``), not
    here - and ``socketpair`` is allowed because asyncio and multiprocessing
    cannot start without it. Every other denial is the same.
    """
    library = ctypes.util.find_library("seccomp")
    if library is None:
        raise RuntimePolicyError("seccomp_unavailable", "libseccomp is required for Linux execution.")
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(_Compare)]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    context = lib.seccomp_init(ALLOW)
    if not context:
        raise RuntimePolicyError("seccomp_unavailable", "Cannot initialize native seccomp filter.")
    output = tempfile.TemporaryFile()
    try:
        def deny(name, code=errno.EPERM, comparison=None, optional=False):
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number == -1:
                if optional:
                    return
                raise RuntimePolicyError("seccomp_unavailable", "Required syscall rule is unavailable.")
            values = comparison if isinstance(comparison, list) else [comparison] if comparison is not None else []
            pointer = (_Compare * len(values))(*values) if values else None
            if lib.seccomp_rule_add_array(context, ERRNO | code, number, len(values), pointer) < 0:
                raise RuntimePolicyError("seccomp_unavailable", "Cannot install required syscall rule.")

        if loopback_tcp and not restricted:
            # AF_INET/AF_INET6 stream sockets only, in a fresh loopback-only netns.
            deny("socket", comparison=_Compare(0, 2, 2, 0))
            deny("socket", comparison=_Compare(0, 6, 10, 0))
            for family in range(3, 10):
                deny("socket", comparison=_Compare(0, 4, family, 0))
            for kind in range(16):
                if kind != 1:
                    deny("socket", comparison=_Compare(1, 7, 15, kind))
        elif not restricted:
            deny("socket"); deny("connect")
        if not restricted:
            deny("socketpair")
        for name in ("mount", "umount2", "pivot_root", "chroot",
                     "unshare", "setns", "ptrace", "process_vm_readv", "process_vm_writev",
                     "bpf", "perf_event_open", "keyctl", "add_key", "request_key", "open_by_handle_at",
                     "io_uring_setup", "mknod", "mknodat", "reboot", "kexec_load", "swapon", "swapoff"):
            deny(name)
        for name in ("fsopen", "fsconfig", "fsmount", "open_tree", "move_mount", "mount_setattr", "userfaultfd", "fanotify_init"):
            deny(name, optional=True)
        deny("clone3", errno.ENOSYS, optional=True)
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000, 0x40000000):
            deny("clone", comparison=_Compare(0, 7, flag, flag))
        if lib.seccomp_export_bpf(context, output.fileno()) < 0:
            raise RuntimePolicyError("seccomp_unavailable", "Cannot export native seccomp filter.")
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        lib.seccomp_release(context)
