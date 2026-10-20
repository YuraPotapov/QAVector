"""The only OS detection/selection point; no provider downgrade or host fallback."""

import importlib
import sys

from ..errors import RuntimePolicyError

PROVIDERS = {
    "linux-isolated": ("linux", "execution_runtime.backends.linux", "BundledBubblewrapBackend"),
    "linux-system-isolated": ("linux", "execution_runtime.backends.linux", "SystemBubblewrapBackend"),
    "windows-isolated": ("windows", "execution_runtime.backends.windows", "WindowsBackend"),
}


def host_platform():
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    return "unsupported"


class BackendRegistry:
    def select(self, provider="auto", host=None):
        platform = host_platform() if host is None else host
        if platform not in ("linux", "windows"):
            raise RuntimePolicyError("unsupported_platform", "No isolation backend exists for this OS.")
        selected = platform + "-isolated" if provider == "auto" else provider
        if not isinstance(selected, str) or selected not in PROVIDERS:
            raise RuntimePolicyError("unknown_provider", "Unknown isolation provider; no compatibility fallback was selected.")
        expected, module, name = PROVIDERS[selected]
        if expected != platform:
            raise RuntimePolicyError("provider_platform_mismatch", "The requested backend does not support this OS.")
        return getattr(importlib.import_module(module), name)()
