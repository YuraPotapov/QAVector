"""Transient secret inputs, kept separate from serializable policy and artifacts."""

from dataclasses import dataclass, field
import hashlib
import re

from .errors import RuntimePolicyError


@dataclass(frozen=True, init=False)
class SecretValues:
    _items: tuple = field(repr=False)

    def __init__(self, values=None):
        values = {} if values is None else values
        if not isinstance(values, dict) or len(values) > 32:
            raise RuntimePolicyError("invalid_secret_inputs", "Expected at most 32 explicit secret values.")
        total = 0
        for name, value in values.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:/-]{0,255}", name)
                    or not isinstance(value, str) or not value):
                raise RuntimePolicyError("invalid_secret_inputs", "Secret inputs require grant identifiers and nonempty text values.")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeError:
                raise RuntimePolicyError("invalid_secret_inputs", "Secret inputs must contain valid UTF-8 text.") from None
            total += size
            if size > 16384 or total > 131072:
                raise RuntimePolicyError("secret_inputs_too_large", "Secret inputs exceed their per-value or aggregate bound.")
        object.__setattr__(self, "_items", tuple(sorted(values.items())))

    @property
    def names(self):
        return tuple(name for name, _ in self._items)

    def items(self):
        """Explicit access for the trusted native delivery adapter only."""
        return self._items


def secret_file_name(name):
    """An identifier may contain '/', so it must never become a relative path."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()
