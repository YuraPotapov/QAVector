"""Anonymous sealed secret files; native descriptors never cross into workload exec."""

import os

from ...errors import RuntimePolicyError
from ...secrets import secret_file_name

SECRET_DIRECTORY = "/run/qavector-secrets"


def secret_path(name):
    return SECRET_DIRECTORY + "/" + secret_file_name(name)


def sealed_secret(name, value):
    import fcntl
    if not hasattr(os, "memfd_create"):
        raise RuntimePolicyError("secret_delivery_unavailable", "Anonymous sealed secret files are unavailable.")
    fd = os.memfd_create("qavector-secret", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        pending = memoryview(value.encode("utf-8"))
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError("Secret input could not be written")
            pending = pending[written:]
        os.lseek(fd, 0, os.SEEK_SET)
        os.fchmod(fd, 0o400)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS,
                    fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
        return fd, secret_path(name)
    except BaseException:
        os.close(fd)
        raise
