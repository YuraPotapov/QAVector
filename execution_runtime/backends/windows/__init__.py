"""Windows extension point with no Win32 imports or pretend sandbox support."""

from ..._json import Snapshot
from ..base import Capability, CapabilitySet, UnimplementedBackend


class WindowsBackend(UnimplementedBackend):
    provider = "windows-isolated"
    platform = "windows"

    def capabilities(self):
        features = (Capability("runtime_execution", "unsupported",
                               "Windows native launch and access isolation are planned, not implemented."),)
        return CapabilitySet(self.provider, self.platform, features,
                             Snapshot({"native_execution_implemented": False,
                                       "native_probes_performed": False}))
