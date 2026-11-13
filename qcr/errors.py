"""Public, value-free errors at the capability boundary."""


class QCRError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)
