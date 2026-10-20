"""Lexical platform paths and trusted workspace bindings, without host IO.

These checks are schema validation, not an OS security boundary. The native
launcher must bind/verify actual filesystem objects again before execution.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from ._json import object_fields
from .errors import RuntimePolicyError

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise RuntimePolicyError("invalid_identifier", "Expected a simple identifier.", field)
    return value


def relative_path(value, field="filesystem.path"):
    if not isinstance(value, str) or not value or any(c in value for c in ("\\", ":", "\0")):
        raise RuntimePolicyError("invalid_path", "Expected a portable relative path.", field)
    if value != "." and any(p in ("", ".", "..") for p in value.split("/")):
        raise RuntimePolicyError("invalid_path", "Relative paths cannot traverse or contain empty components.", field)
    if value.startswith("/") or "${" in value:
        raise RuntimePolicyError("invalid_path", "Expected a resolved relative path.", field)
    return value


def contains_path(parent, child):
    """Logical grant paths are portable, case-sensitive, slash-separated names."""
    return parent == "." or child == parent or child.startswith(parent + "/")


@dataclass(frozen=True)
class PathBinding:
    name: str
    source: str
    target: str

    def __post_init__(self):
        identifier(self.name, "bindings.name")
        if not isinstance(self.source, str) or not isinstance(self.target, str):
            raise RuntimePolicyError("invalid_binding", "Binding paths must be immutable strings.")

    def to_document(self):
        return {"name": self.name, "source": self.source, "target": self.target}


class PathMapper:
    def __init__(self, platform):
        if platform not in ("linux", "windows"):
            raise RuntimePolicyError("unsupported_platform", "No path mapper for this platform.")
        self.platform = platform
        self.path_type = PureWindowsPath if platform == "windows" else PurePosixPath

    def absolute(self, value, field, *, allow_root=False):
        if not isinstance(value, str) or not value or "\0" in value or "${" in value:
            raise RuntimePolicyError("invalid_path", "Expected a resolved absolute path.", field)
        path = self.path_type(value)
        if not path.is_absolute() or ".." in path.parts or (not allow_root and path == self.path_type(path.anchor)):
            raise RuntimePolicyError("invalid_path", "Expected an absolute path below a filesystem root.", field)
        if self.platform == "linux":
            if value.startswith("//") or "\\" in value:
                raise RuntimePolicyError("invalid_path", "Ambiguous POSIX path.", field)
        else:
            # Network/device paths need their own verified access semantics.
            if path.drive.startswith("\\"):
                raise RuntimePolicyError("unsupported_path", "UNC and device paths are not supported yet.", field)
            for part in path.parts[1:]:
                if (part.endswith((".", " ")) or ":" in part or any(ord(c) < 32 for c in part) or
                        any(c in part for c in '<>"|?*') or
                        _WINDOWS_RESERVED.match(part)):
                    raise RuntimePolicyError("invalid_path", "Ambiguous or reserved Windows path.", field)
        return str(path)

    def bindings(self, raw):
        if not isinstance(raw, list):
            raise RuntimePolicyError("invalid_type", "bindings must be an array.", "bindings")
        if len(raw) > 256:
            raise RuntimePolicyError("too_many_bindings", "bindings cannot exceed 256 entries.", "bindings")
        result = []
        for entry in raw:
            object_fields(entry, ("name", "source", "target"), "bindings",
                          ("name", "source", "target"))
            binding = PathBinding(identifier(entry["name"], "bindings.name"),
                                  self.absolute(entry["source"], "bindings.source"),
                                  self.absolute(entry["target"], "bindings.target"))
            if any(one.name == binding.name for one in result):
                raise RuntimePolicyError("duplicate_binding", "Binding names must be unique.", "bindings")
            target = self.path_type(binding.target)
            for previous in result:
                other = self.path_type(previous.target)
                if target == other or target in other.parents or other in target.parents:
                    raise RuntimePolicyError("overlapping_targets", "Binding targets cannot overlap.", "bindings")
            result.append(binding)
        return tuple(sorted(result, key=lambda one: one.name))

    def resolve(self, binding, subpath):
        relative_path(subpath)
        source = self.path_type(binding.source).joinpath(*PurePosixPath(subpath).parts)
        target = self.path_type(binding.target).joinpath(*PurePosixPath(subpath).parts)
        return {"source": self.absolute(str(source), "filesystem.source"),
                "target": self.absolute(str(target), "filesystem.target")}
