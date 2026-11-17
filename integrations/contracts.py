"""Provider-owned operation declarations, independent of every consumer."""

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

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
    #: Input fields that name a file on this computer: a caller checks them
    #: against where the asker may read before anything else happens.
    local_paths: tuple = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ValueError("Invalid operation name.")
        if not isinstance(self.description, str) or not self.description or len(self.description) > 256:
            raise ValueError("Invalid operation description.")
        if self.access not in ("read", "write", "local_write") or self.risk not in ("low", "medium", "high", "critical"):
            raise ValueError("Invalid operation access or risk.")
        if type(self.idempotent) is not bool or type(self.version) is not int or self.version < 1:
            raise ValueError("Invalid operation version or idempotency.")
        if not isinstance(self.local_paths, tuple) or any(
                not isinstance(name, str) or name not in (self.input_schema.get("properties") or {})
                for name in self.local_paths):
            raise ValueError("Local paths name input fields.")
        validate_schema(self.input_schema)
        validate_schema(self.output_schema)


@dataclass(frozen=True)
class Provider:
    """``prepare`` and ``write`` are needed only for ``write`` contracts:

    * ``prepare(name, connection, token, request, get)`` reads what a person
      needs to decide on the write and returns ``{"request", "card", "noop"}``:
      the request made exact (it still satisfies the input schema), what to
      show, and the result when there is nothing to do (``None`` otherwise).
      The card is ``{operation, key, summary, title, changes: [{label, from,
      to}], notes: [text], edit: {field: {label, value, was, multiline}}}``;
      ``edit`` names the request fields the person may change before
      approving - a dotted name for a nested one (``fields.summary``);
    * ``write(name, connection, token, request, get)`` makes the change.

    Both raise :class:`WriteError` for a refusal of their own.
    """
    name: str
    contracts: Mapping[str, OperationContract]
    operations: Mapping[str, Callable]
    read: Callable
    prepare: Optional[Callable] = None
    write: Optional[Callable] = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", self.name) or not callable(self.read):
            raise ValueError("Invalid provider definition.")
        for name, contract in self.contracts.items():
            if not isinstance(contract, OperationContract) or contract.name != name or not callable(self.operations.get(name)):
                raise ValueError("Operation contract has no matching provider operation.")
        if any(contract.access == "write" for contract in self.contracts.values()) and not (
                callable(self.prepare) and callable(self.write)):
            raise ValueError("A provider with writes prepares and makes them.")


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


#: Why a write was not made - or is not known to be. The read codes apply too.
#: Only the codes in :data:`WRITE_SAYS` may carry a sentence of the provider's
#: own making (names of transitions and fields), never a service's response text.
WRITE_ERRORS = dict(READ_ERRORS, **{
    "denied": "The person did not approve the change.",
    "fields_required": "The change needs fields the request did not give.",
    "conflict": "The item changed after it was read; read it again.",
    "refused": "The change was refused as invalid.",
    "not_offered": "The item does not offer that change now.",
    "unknown_outcome": "The service could not be reached after the change was sent, so whether it was "
                       "made is unknown. Read the item before trying again.",
    "failed": "Integration write failed.",
})
WRITE_SAYS = ("fields_required", "conflict", "refused", "not_offered")


class WriteError(ValueError):
    """A write not made, or not known to be made. ``message`` is kept only for
    :data:`WRITE_SAYS` codes; ``details`` is data for the caller to act on
    (the fields a transition asks for), never response text."""

    def __init__(self, code="failed", message=None, details=None):
        if code not in WRITE_ERRORS:
            code = "failed"
        self.code = code
        self.details = dict(details or {})
        said = message if code in WRITE_SAYS and isinstance(message, str) and message.strip() else None
        super().__init__((said or WRITE_ERRORS[code])[:1000])
