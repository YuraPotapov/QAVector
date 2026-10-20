"""One supervised service owner; a crashed owner's live instance blocks replacement."""

import hashlib
import os

from ..._json import read_document
from ...errors import RuntimePolicyError
from ...registry import _atomic
from .filesystem import open_pinned
from .recovery import INSTANCE_ID, lock_file, recover_instance


class ServiceScope:
    def __init__(self, backend, key):
        if not isinstance(key, str) or not key or len(key) > 8192:
            raise RuntimePolicyError("invalid_service_identity", "Expected a bounded control-plane service identity.")
        self.backend = backend
        self.name = hashlib.sha256(key.encode("utf-8")).hexdigest()
        self.fd = None
        self.path = None

    def __enter__(self):
        self.backend._state()
        directory = self.backend.state_directory / "services"
        directory.mkdir(mode=0o700, exist_ok=True)
        fd = open_pinned(str(directory), directory=True)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise RuntimePolicyError("unsafe_service_state", "Service ownership storage must be private.")
        finally:
            os.close(fd)
        self.path = directory / (self.name + ".json")
        try:
            self.fd = lock_file(directory / (self.name + ".lock"), create=True, nonblocking=True)
            if self.path.exists() or self.path.is_symlink():
                if self.path.is_symlink():
                    raise RuntimePolicyError("unsafe_service_state", "Service ownership records cannot be symlinks.")
                record = read_document(self.path)
                if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("provider") != self.backend.provider:
                    raise RuntimePolicyError("unsafe_service_state", "Service ownership record has an unknown format.")
                instance = record.get("instance_id", "")
                if instance:
                    if not isinstance(instance, str) or not INSTANCE_ID.fullmatch(instance):
                        raise RuntimePolicyError("unsafe_service_state", "Service ownership instance identity is invalid.")
                    previous = self.backend.state_directory / instance
                    if previous.is_symlink() or not previous.is_dir():
                        raise RuntimePolicyError("unsafe_service_state", "Previous service state is missing or unsafe.")
                    lease = lock_file(previous / "instance.lock", nonblocking=True)
                    try:
                        recover_instance(previous, self.backend.cgroup_parent, provider=self.backend.provider)
                    finally:
                        os.close(lease)
            return self
        except BlockingIOError:
            self.__exit__(None, None, None)
            raise RuntimePolicyError("service_already_owned", "This service still has a live supervisor or isolated descendant.") from None
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def bind(self, handle):
        self.backend._get(handle)
        _atomic(self.path, {"schema_version": 1, "provider": self.backend.provider,
                           "instance_id": handle.instance_id, "policy_digest": handle.policy_digest})

    def clear(self):
        _atomic(self.path, {"schema_version": 1, "provider": self.backend.provider, "instance_id": ""})

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
