"""Versioned environment profiles with immutable revisions and guarded updates."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ._json import Snapshot, canonical, object_fields, read_document
from .errors import RuntimePolicyError
from .policy import PolicyPatch

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_REVISION = re.compile(r"^[0-9a-f]{64}$")


def profile_name(value):
    if (not isinstance(value, str) or not _NAME.fullmatch(value) or
            re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", value)):
        raise RuntimePolicyError("invalid_profile_name", "Expected a portable lowercase profile name.", "metadata.name")
    return value


def revision_name(value):
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise RuntimePolicyError("invalid_revision", "Expected a 64-character lowercase SHA-256 revision.")
    return value


@dataclass(frozen=True, init=False)
class EnvironmentProfile(Snapshot):
    def __init__(self, document):
        object_fields(document, ("api_version", "kind", "metadata", "spec"), "profile",
                      ("api_version", "kind", "metadata", "spec"))
        if document["api_version"] != "qavector.io/v1" or document["kind"] != "ExecutionEnvironment":
            raise RuntimePolicyError("unsupported_schema", "Expected qavector.io/v1 ExecutionEnvironment.")
        meta, spec = document["metadata"], document["spec"]
        object_fields(meta, ("name", "description"), "metadata", ("name",))
        name = profile_name(meta["name"])
        description = meta.get("description", "")
        if not isinstance(description, str) or len(description) > 4096:
            raise RuntimePolicyError("invalid_description", "Description must be text up to 4096 characters.", "metadata.description")
        object_fields(spec, ("provider", "policy"), "spec", ("provider", "policy"))
        if spec["provider"] not in ("auto", "linux-isolated", "linux-system-isolated", "windows-isolated"):
            raise RuntimePolicyError("unknown_provider", "Unknown isolation provider.", "spec.provider")
        policy = PolicyPatch(spec["policy"])
        super().__init__({"api_version": "qavector.io/v1", "kind": "ExecutionEnvironment",
                          "metadata": {"name": name, "description": description},
                          "spec": {"provider": spec["provider"], "policy": policy.to_document()}})

    @property
    def name(self):
        return self.to_document()["metadata"]["name"]

    @property
    def provider(self):
        return self.to_document()["spec"]["provider"]

    @property
    def policy(self):
        return PolicyPatch(self.to_document()["spec"]["policy"])


@dataclass(frozen=True)
class ProfileRecord:
    profile: EnvironmentProfile
    revision: str

    def to_document(self):
        return {"revision": self.revision, "profile": self.profile.to_document()}


def default_directory():
    # Lazy: standalone model/contract users need neither the launcher nor Qt.
    import runtime_paths
    return os.path.join(runtime_paths.user_data_root(), "execution-environments")


def _atomic(path, document):
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical(document) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class EnvironmentRegistry:
    """Control-plane storage, never an authorization source inside a workload.

    A per-profile writer lock and compare-and-swap revision prevent lost updates.
    A stale lock fails explicitly; it is never stolen from another writer.
    Native launch preparation must also reject writable workload access here.
    """

    def __init__(self, directory=None):
        self.directory = Path(default_directory() if directory is None else directory).absolute()

    def _directory(self, name, create=False):
        profile_name(name)
        directory = self.directory / name
        # Do not follow a profile or registry symlink into another store.
        for candidate in (self.directory, directory):
            if candidate.is_symlink():
                raise RuntimePolicyError("unsafe_registry_path", "Registry directories cannot be symlinks.")
        if create:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def _head(self, directory):
        path = directory / "current.json"
        if path.is_symlink():
            raise RuntimePolicyError("unsafe_registry_path", "Registry records cannot be symlinks.")
        if not path.exists():
            return ""
        raw = read_document(path)
        object_fields(raw, ("revision",), "registry.current", ("revision",))
        return revision_name(raw["revision"])

    def load(self, name, revision=None):
        directory = self._directory(name)
        selected = revision_name(revision) if revision is not None else self._head(directory)
        if not selected:
            raise RuntimePolicyError("profile_not_found", "Environment profile was not found.")
        path = directory / (selected + ".json")
        if path.is_symlink():
            raise RuntimePolicyError("unsafe_registry_path", "Registry records cannot be symlinks.")
        raw = read_document(path)
        object_fields(raw, ("schema_version", "revision", "profile"), "registry.revision",
                      ("schema_version", "revision", "profile"))
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise RuntimePolicyError("unsupported_schema", "Unsupported registry schema.")
        profile = EnvironmentProfile(raw["profile"])
        if profile.name != name or raw["revision"] != selected or profile.digest != selected:
            raise RuntimePolicyError("revision_mismatch", "Profile revision or identity does not match its content.")
        return ProfileRecord(profile, selected)

    def list(self):
        if self.directory.is_symlink():
            raise RuntimePolicyError("unsafe_registry_path", "Registry directories cannot be symlinks.")
        if not self.directory.exists():
            return ()
        records = []
        for entry in sorted(self.directory.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                raise RuntimePolicyError("unsafe_registry_path", "Registry directories cannot be symlinks.")
            if entry.is_dir():
                # Ignore an unpublished profile only while no head exists.
                directory = self._directory(entry.name)
                if self._head(directory):
                    records.append(self.load(entry.name))
        return tuple(records)

    def save(self, profile, expected_revision=None):
        if not isinstance(profile, EnvironmentProfile):
            raise RuntimePolicyError("invalid_profile", "Expected a validated EnvironmentProfile.")
        if expected_revision not in (None, ""):
            revision_name(expected_revision)
        directory = self._directory(profile.name, create=True)
        lock = directory / ".writer-lock"
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimePolicyError("registry_busy", "Another profile writer holds the lock; retry after it finishes.") from None
        try:
            current = self._head(directory)
            if expected_revision is not None and expected_revision != current:
                raise RuntimePolicyError("revision_conflict", "The profile changed; reload before saving.")
            if current:
                self.load(profile.name, current)  # Never repair corruption by overwriting it.
                if current == profile.digest:
                    return ProfileRecord(profile, current)
                if expected_revision is None:
                    raise RuntimePolicyError("revision_required", "Updating a profile requires its current revision.")
            revision = profile.digest
            path = directory / (revision + ".json")
            if path.exists() or path.is_symlink():
                self.load(profile.name, revision)
            else:
                _atomic(path, {"schema_version": 1, "revision": revision, "profile": profile.to_document()})
            _atomic(directory / "current.json", {"revision": revision})
            return ProfileRecord(profile, revision)
        finally:
            lock.rmdir()
