"""Structured diagnostics without echoing untrusted values or credentials."""


class RuntimePolicyError(ValueError):
    def __init__(self, code, message, field="", layer=""):
        super().__init__(message)
        self.code = code
        self.field = field
        self.layer = layer

    def to_document(self):
        return {"code": self.code, "message": str(self),
                "field": self.field, "layer": self.layer}
