"""Bounded JSON-RPC 2.0 line framing shared by host and worker."""

import json

from .validation import ContractError, validate_contract

MAX_FRAME_BYTES = 256 * 1024
PROTOCOL_VERSION = "1.0.0"


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContractError("WORKER_PROTOCOL_ERROR", "Duplicate JSON key")
        value[key] = item
    return value


def _constant(value):
    raise ContractError("WORKER_PROTOCOL_ERROR", "Non-finite JSON number")


def decode(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ContractError("WORKER_PROTOCOL_ERROR", "Invalid or oversized protocol frame")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        validate_contract("plugin-rpc", value)
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError("WORKER_PROTOCOL_ERROR", "Invalid protocol message") from None
    return value


def encode(message):
    try:
        validate_contract("plugin-rpc", message)
        raw = (json.dumps(message, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, RecursionError):
        raise ContractError("WORKER_PROTOCOL_ERROR", "Invalid protocol message") from None
    if len(raw) > MAX_FRAME_BYTES:
        raise ContractError("WORKER_PROTOCOL_ERROR", "Protocol message exceeds frame limit")
    return raw


def failure(code, message, *, side_effects="unknown"):
    return {"schema_version": "1.0.0", "code": code, "message": message,
            "retryable": False, "side_effects": side_effects, "details": {}}


def error_response(identifier, code, message, rpc_code=-32000):
    return {"jsonrpc": "2.0", "id": identifier, "error": {
        "code": rpc_code, "message": message, "data": failure(code, message)}}
