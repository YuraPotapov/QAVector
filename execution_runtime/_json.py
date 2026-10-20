"""Bounded, duplicate-free JSON and immutable canonical value snapshots."""

import hashlib
import json
from dataclasses import dataclass

from .errors import RuntimePolicyError

MAX_DOCUMENT_BYTES = 1024 * 1024


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise RuntimePolicyError("invalid_document", "Expected finite JSON values.") from None


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimePolicyError("duplicate_key", "Duplicate JSON keys are not allowed.")
        result[key] = value
    return result


def _constant(_value):
    raise RuntimePolicyError("invalid_number", "JSON numbers must be finite.")


def read_document(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    except (OSError, ValueError):
        raise RuntimePolicyError("document_unreadable", "Cannot read the JSON document.") from None
    return parse_document(raw)


def parse_document(raw):
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise RuntimePolicyError("document_too_large", "JSON document exceeds 1 MiB.")
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except RuntimePolicyError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimePolicyError("invalid_json", "Expected valid UTF-8 JSON.") from None


def object_fields(value, allowed, field, required=()):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise RuntimePolicyError("invalid_type", "Expected an object.", field)
    if set(value) - set(allowed):
        raise RuntimePolicyError("unknown_field", "Unexpected fields; allowed: %s."
                                 % ", ".join(sorted(allowed)), field)
    if set(required) - set(value):
        raise RuntimePolicyError("missing_field", "Missing required fields: %s."
                                 % ", ".join(sorted(set(required) - set(value))), field)
    return value


@dataclass(frozen=True, init=False)
class Snapshot:
    """Canonical JSON storage has no mutable nested objects to escape through."""

    _canonical: str

    def __init__(self, value):
        object.__setattr__(self, "_canonical", canonical(value))

    def to_document(self):
        return json.loads(self._canonical)

    @property
    def digest(self):
        return hashlib.sha256(self._canonical.encode("utf-8")).hexdigest()
