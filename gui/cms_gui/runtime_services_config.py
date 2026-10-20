"""GUI-side shape checks; the core remains authoritative for policy enforcement."""

import re


def execution_problems(execution):
    if (not isinstance(execution, dict) or execution.get("mode") != "isolated"
            or set(execution) - {"mode", "deployment", "revision", "overrides"}):
        return ["Execution environment configuration is invalid; it cannot fall back to a host launch."]
    if not isinstance(execution.get("deployment"), str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", execution["deployment"]):
        return ["Choose an approved execution environment."]
    if not isinstance(execution.get("revision"), str) or not re.fullmatch(r"[0-9a-f]{64}", execution["revision"]):
        return ["Review and select the current execution environment revision."]
    if "overrides" in execution and not isinstance(execution["overrides"], dict):
        return ["Execution environment overrides must be an object."]
    return []
