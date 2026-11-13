"""In-process authority. Only the trusted composition root issues callers.

There is intentionally no from_document/permissions-on-request interface.
These objects are not a sandbox against hostile Python in the host process.
External transports must authenticate before asking this authority to issue.
"""

from dataclasses import dataclass
from typing import Optional

from .contracts import text
from .errors import QCRError


@dataclass(frozen=True)
class Grant:
    project_id: str
    permission: str
    resource_id: Optional[str] = None

    def __post_init__(self):
        text(self.project_id, "project id")
        text(self.permission, "permission")
        if self.resource_id is not None:
            text(self.resource_id, "resource id")


@dataclass(frozen=True, eq=False)
class Caller:
    actor_id: str


class Authority:
    def __init__(self):
        self._callers = {}

    def issue(self, actor_id, grants=()):
        text(actor_id, "actor id")
        grants = frozenset(grants)
        if any(not isinstance(grant, Grant) for grant in grants):
            raise QCRError("invalid_contract", "Expected trusted grants.")
        caller = Caller(actor_id)
        self._callers[caller] = grants
        return caller

    def revoke(self, caller):
        if isinstance(caller, Caller):
            self._callers.pop(caller, None)

    def check_caller(self, caller):
        if not isinstance(caller, Caller) or caller not in self._callers:
            raise QCRError("authorization_denied", "Caller is not authorized.")

    def allows(self, caller, ref, permission):
        self.check_caller(caller)
        return any(g.project_id == ref.project_id and g.permission == permission
                   and (g.resource_id is None or g.resource_id == ref.resource_id)
                   for g in self._callers[caller])

    def require(self, caller, ref, permission):
        if not self.allows(caller, ref, permission):
            raise QCRError("authorization_denied", "Resource is not accessible.")
