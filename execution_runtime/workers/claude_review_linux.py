"""Pinned bootstrap for the SDK Claude backend over the provider gateway."""

import signal
import sys

# This path is populated by the approved image builder and checked against the
# core's bundled inventory before launch. Never resolve backend code on host PATH.
sys.path.insert(0, "/qavector/site")


def main():
    from qavector_agent_claude.gateway import main as execute
    return execute()


if __name__ == "__main__":
    def interrupted(_signal, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
