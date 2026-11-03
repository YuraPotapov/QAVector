"""Host API dispatch with identity, attempt, scoped handle and policy checks."""

from qavector_sdk import ContractError, validate_contract


class HostGateway:
    def __init__(self, execution_id, attempt_id, grants, authorize):
        # Host-owned grants: handle -> operation -> trusted service callback.
        # authorize must check the current effective policy on every call.
        self.execution_id, self.attempt_id = execution_id, attempt_id
        self.grants = {handle: dict(operations) for handle, operations in grants.items()}
        self.authorize = authorize
        self.closed = False

    def close(self):
        self.closed = True
        self.grants.clear()

    def call(self, params):
        validate_contract("plugin-host-call", params)
        if (self.closed or params["execution_id"] != self.execution_id
                or params["attempt_id"] != self.attempt_id):
            raise ContractError("POLICY_UNSATISFIED", "Host API execution scope is unavailable")
        operation, handle = params["operation"], params["handle"]
        callback = self.grants.get(handle, {}).get(operation)
        if callback is None or self.authorize(operation, params["arguments"]) is not True:
            raise ContractError("POLICY_UNSATISFIED", "Host API operation is not granted by effective policy")
        return callback(params["arguments"])
