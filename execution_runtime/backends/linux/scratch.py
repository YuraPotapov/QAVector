"""Scratch means aggregate allocated tmpfs data pages, never a per-file limit."""

import os

from ...errors import RuntimePolicyError


def scratch_size(resources):
    size = resources.get("scratch_bytes")
    if size is not None and (type(size) is not int or size <= 0
            or size > 2 ** 63 - os.sysconf("SC_PAGE_SIZE")
            or size % os.sysconf("SC_PAGE_SIZE")):
        raise RuntimePolicyError("unsupported_scratch_limit", "Scratch capacity must be a positive page-aligned byte limit.")
    return size


def cgroup_limits(resources):
    return {name: value for name, value in resources.items() if name != "scratch_bytes"}
