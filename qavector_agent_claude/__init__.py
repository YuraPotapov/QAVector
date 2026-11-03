"""Bundled Claude backend; depends on the public SDK, never private core."""

__all__ = ["ClaudeCliBackend"]


def __getattr__(name):
    # Metadata/command discovery must not import the backend or SDK validator.
    if name == "ClaudeCliBackend":
        from .backend import ClaudeCliBackend
        return ClaudeCliBackend
    raise AttributeError(name)
