"""Reserved explicit non-isolated provider; no such authorization profile exists yet."""

from .._json import Snapshot
from .base import Capability, CapabilitySet, UnimplementedBackend


class NativeProcessBackend(UnimplementedBackend):
    provider = "native-process"

    def __init__(self, platform):
        self.platform = platform

    def capabilities(self):
        return CapabilitySet(self.provider, self.platform, (
            Capability("runtime_execution", "unsupported", "Explicit non-isolated execution profiles are not enabled."),),
            Snapshot({"unrestricted_profiles_enabled": False}))
