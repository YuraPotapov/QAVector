"""Make the project root importable for the test suite regardless of how pytest
is invoked, so ``from engine import ...`` / ``from domain import ...`` resolve
the top-level packages (there is no installed package)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pytest_addoption(parser):
    parser.addoption("--linux-runtime-integration", action="store_true",
                     help="Require real Linux namespace/seccomp integration tests; missing prerequisites fail.")
    parser.addoption("--linux-runtime-resource-integration", action="store_true",
                     help="Require native cgroup CPU/memory/task pressure tests; needs explicit delegation.")
    parser.addoption("--linux-runtime-cgroup-parent", default=None,
                     help="Existing writable delegated cgroup v2 parent; tests never configure the host parent.")
    parser.addoption("--linux-runtime-scratch-integration", action="store_true",
                     help="Require kernel tmpfs scratch quota acceptance; launcher needs --size.")
    parser.addoption("--linux-runtime-launcher", default=None,
                     help="Explicit approved Bubblewrap executable for native tests; privately copied and hash-pinned.")
    parser.addoption("--linux-runtime-bundle-dir", default=None,
                     help="Built QAVector runtime resource directory; native acceptance uses the bundled backend.")
    parser.addoption("--runtime-frozen-core", default=None,
                     help="Built core executable for real service protocol/namespace acceptance.")
    parser.addoption("--runtime-claude-cli", default=None,
                     help="Explicit installed Claude CLI to pin in the local TLS integration fixture; no account call.")
