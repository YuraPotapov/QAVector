"""Provider-owned operation declarations, independent of every consumer."""

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from qavector_sdk import validate_schema


@dataclass(frozen=True)
class OperationContract:
    name: str
    description: str
    input_schema: Any
    output_schema: Any
    access: str = "read"
    risk: str = "low"
    idempotent: bool = True
    version: int = 1

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ValueError("Invalid operation name.")
        if not isinstance(self.description, str) or not self.description or len(self.description) > 256:
            raise ValueError("Invalid operation description.")
        if self.access not in ("read", "write", "local_write") or self.risk not in ("low", "medium", "high", "critical"):
            raise ValueError("Invalid operation access or risk.")
        if type(self.idempotent) is not bool or type(self.version) is not int or self.version < 1:
            raise ValueError("Invalid operation version or idempotency.")
        validate_schema(self.input_schema)
        validate_schema(self.output_schema)


@dataclass(frozen=True)
class Provider:
    name: str
    contracts: Mapping[str, OperationContract]
    operations: Mapping[str, Callable]
    read: Callable

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", self.name) or not callable(self.read):
            raise ValueError("Invalid provider definition.")
        for name, contract in self.contracts.items():
            if not isinstance(contract, OperationContract) or contract.name != name or not callable(self.operations.get(name)):
                raise ValueError("Operation contract has no matching provider operation.")


#: Why a read failed, as a caller can act on it - one fixed sentence each, so
#: no token, site, issue text or provider wording can ride along in an error.
READ_ERRORS = {
    "unsupported": "Operation is not exposed as a read.",
    "unavailable": "Connection is unavailable.",
    "no_token": "Connection has no token.",
    "signin": "Connection needs signing in again.",
    "scope_invalid": "Connection's predefined query cannot be resolved.",
    "out_of_scope": "Item is not available in this connection's scope.",
    "invalid_request": "Request was refused as invalid.",
    "auth": "Service refused the connection's credentials or permissions.",
    "not_found": "Service did not find the item.",
    "rate_limited": "Service is limiting requests; try again later.",
    "network": "Service could not be reached.",
    "service_error": "Service failed to answer the request.",
    "invalid_response": "Service answered outside the operation's contract.",
    "failed": "Integration read failed.",
}


class ReadError(ValueError):
    """An intentionally value-free refusal at the project credential boundary.

    ``code`` is one of :data:`READ_ERRORS`, and the message is always that
    code's fixed sentence - never text from the exception that caused it.
    """

    def __init__(self, code="failed"):
        if code not in READ_ERRORS:
            code = "failed"
        self.code = code
        super().__init__(READ_ERRORS[code])
