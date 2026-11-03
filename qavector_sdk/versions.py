"""Stable numeric versions and comparator intersections used by Plugin API v1.

Prereleases, build metadata, wildcards and Python version specifiers are not
part of this grammar. Python wheel compatibility uses a separate resolver.
"""

import re
from operator import eq, ge, gt, le, lt

from .validation import ContractError

_VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_OPERATORS = {"==": eq, ">=": ge, ">": gt, "<=": le, "<": lt}


def version(value):
    match = re.fullmatch(_VERSION, value) if isinstance(value, str) else None
    if match is None:
        raise ContractError("PLUGIN_API_INCOMPATIBLE", "Expected stable major.minor.patch version")
    return tuple(int(part) for part in match.groups())


def satisfies(value, constraint):
    actual = version(value)
    if not isinstance(constraint, str) or not constraint:
        raise ContractError("PLUGIN_API_INCOMPATIBLE", "Expected version comparator intersection")
    matches = []
    for clause in constraint.split(","):
        match = re.fullmatch(r"(==|>=|<=|>|<)(" + _VERSION + r")", clause)
        if match is None:
            raise ContractError("PLUGIN_API_INCOMPATIBLE", "Unsupported version constraint syntax")
        matches.append(_OPERATORS[match.group(1)](actual, version(match.group(2))))
    return all(matches)
