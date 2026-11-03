"""Pure capability checks, separate from Runtime authorization and enforcement."""

from .validation import (ContractError, DIALECT, validate_contract,
                         validate_output, validate_schema)


def preflight(profile, request, capabilities, requirements, *, config_schema,
              local_schemas=None):
    """Reject unsupported requirements before an adapter executes work.

    Capabilities must come from describe for this profile/runtime/model, never
    from untrusted profile options. Runtime still checks workspace grants,
    credentials, prerequisites, deadlines and process containment separately.
    """
    for name, document in (("agent-profile", profile), ("agent-request", request),
                           ("agent-capabilities", capabilities),
                           ("agent-requirements", requirements)):
        validate_contract(name, document)
    if request["output_schema"] is not None:
        validate_schema(request["output_schema"])
    for name, ceiling in profile["limits"].items():
        if name not in request["limits"] or request["limits"][name] > ceiling:
            raise ContractError("POLICY_UNSATISFIED", "Request widens profile limit: " + name)
    # Backend option schemas must explicitly close their root object.
    if (not isinstance(config_schema, dict) or config_schema.get("type") != "object"
            or not (config_schema.get("additionalProperties") is False
                    or config_schema.get("unevaluatedProperties") is False)):
        raise ContractError("CONFIG_INVALID", "Backend options require a closed object schema")
    validate_output(profile["backend_options"], config_schema,
                    local_schemas=local_schemas, code="CONFIG_INVALID")

    def require(condition, name):
        if not condition:
            raise ContractError("CAPABILITY_UNSUPPORTED", "Unsatisfied requirement: " + name)

    access = ("none", "read", "write")
    requested_access = requirements.get("workspace_access", "none")
    require(access.index(capabilities["workspace_access"]) >= access.index(requested_access),
            "workspace_access=" + requested_access)
    require(requested_access == "none" or bool(request["workspace_ref"]), "workspace_ref")
    require(request["workspace_ref"] is None or capabilities["workspace_access"] != "none",
            "workspace_access for supplied workspace_ref")
    require("text" in capabilities["content_types"], "text task input")
    for name in ("streaming", "session_resume", "interactive_input"):
        require(not requirements.get(name, False) or capabilities[name], name)
    require(request["session_ref"] is None or capabilities["session_resume"], "session_resume")
    require(not requirements.get("confirmed_cancellation", False)
            or capabilities["cancellation"] == "confirmed", "confirmed_cancellation")
    visibility = ("none", "partial", "full")
    require(visibility.index(capabilities["tool_events"]) >=
            visibility.index(requirements.get("tool_events", "none")), "tool_events")
    for name in ("content_types", "tool_binding"):
        require(set(requirements.get(name, ())) <= set(capabilities[name]), name)
    for name in requirements.get("hard_limits", ()):
        require(capabilities["limits"].get(name) == "hard", "hard limit: " + name)
        require(name in request["limits"], "configured limit: " + name)
    if request["output_schema"] is not None:
        require(capabilities["structured_output"] != "unsupported", "structured_output")
        schema = request["output_schema"]
        dialect = schema.get("$schema", DIALECT) if isinstance(schema, dict) else DIALECT
        require(dialect == DIALECT and dialect in capabilities["schema_dialects"], "schema_dialect")
