"""Cycle-owned role declarations, normalized without importing provider code."""

import copy
from importlib import resources
import json
import re

from cycle.checkpoints import digest


class ProfileError(ValueError):
    pass


def normalize(identifier, declaration):
    from qavector_sdk import ContractError, validate_contract
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]*", identifier):
        raise ProfileError("profile ID must start with a lowercase letter and contain letters, digits, dots, underscores or hyphens")
    if not isinstance(declaration, dict):
        raise ProfileError("profile must be a mapping")
    # Keep normalization out of the saved document. An omitted revision is a
    # stable content digest; explicit revisions never replace content hashing.
    profile = {"schema_version": "1.0.0", "id": identifier, "revision": "pending",
               "backend_version": ">=1.0.0,<2.0.0", "model": None,
               "instructions": "", "credentials_ref": None,
               "limits": {}, "backend_options": {}}
    profile.update(copy.deepcopy(declaration))
    if profile["id"] != identifier:
        raise ProfileError("profile id must match its mapping key")
    try:
        validate_contract("agent-profile", profile)
    except ContractError as exc:
        raise ProfileError("invalid AgentProfile at %s: %s" % (
            ".".join(map(str, exc.path)) or "profile", exc)) from None
    from cycle import variables
    if variables.references(profile):
        raise ProfileError("profiles are static; put variable and step references in the node task or inputs")
    if "revision" not in declaration:
        profile["revision"] = digest({k: v for k, v in profile.items() if k != "revision"})
    return profile


def executable(profile):
    """Validate the currently available trusted Cycle adapter, fail closed.

    Package resolution replaces this single bundled binding in the next step.
    No arbitrary profile entrypoint can be imported by the host process.
    """
    from qavector_sdk import ContractError, validate_output
    from qavector_sdk.versions import satisfies
    from plugin_host.bundled import claude_manifest
    manifest = claude_manifest()
    if profile["backend_ref"] != manifest["contributions"][0]["id"]:
        raise ProfileError("this Cycle adapter currently supports qavector.agent.claude_cli")
    if not satisfies(manifest["version"], profile["backend_version"]):
        raise ProfileError("requested Claude backend version is unavailable")
    if profile["credentials_ref"] is not None:
        raise ProfileError("credential references require the scoped worker transport; the compatibility adapter uses the selected CLI login")
    unsupported = set(profile["limits"]) - {"turns", "wall_time_seconds"}
    if unsupported:
        raise ProfileError("limits unavailable in this adapter: " + ", ".join(sorted(unsupported)))
    schema = json.loads(resources.files("qavector_agent_claude").joinpath(
        "schemas").joinpath("config.json").read_text(encoding="utf-8"))
    try:
        validate_output(profile["backend_options"], schema, code="CONFIG_INVALID")
    except ContractError as exc:
        raise ProfileError("invalid backend_options at %s: %s" % (
            ".".join(map(str, exc.path)) or "backend_options", exc)) from None
    return profile


def selected(cycle, reference):
    if not isinstance(reference, str) or reference not in getattr(cycle, "agent_profiles", {}):
        raise ProfileError("unknown agent_profile; select a key from agent_profiles")
    profile = normalize(reference, cycle.agent_profiles[reference])
    return profile if 'runtime' in cycle.execution else executable(profile)


def problems(cycle):
    found, valid = [], set()
    for identifier, declaration in cycle.agent_profiles.items():
        try:
            normalize(identifier, declaration)
            valid.add(identifier)
        except ProfileError as exc:
            found.append("agent_profiles.%s: %s" % (identifier, exc))
    for step in cycle.steps:
        if step.plugin != "agent":
            continue
        reference = step.settings.get("agent_profile")
        if not isinstance(reference, str) or reference not in cycle.agent_profiles:
            found.append("%s: unknown agent_profile; select a key from agent_profiles" % step.id)
        elif reference in valid:
            try:
                selected(cycle, reference)
            except ProfileError as exc:
                found.append("%s: %s" % (step.id, exc))
    return found


def binding(cycle, step):
    """Fingerprint the referenced content only, including an explicit revision."""
    if step.plugin != "agent":
        return {}
    reference = step.settings.get("agent_profile")
    declaration = cycle.agent_profiles.get(reference) if isinstance(reference, str) else None
    return {"id": reference, "definition": copy.deepcopy(declaration)}
