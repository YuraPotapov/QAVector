"""Portable wire contracts; no model calls, credentials or backend imports."""

import copy
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from qavector_sdk import (ContractError, load_schema, preflight,
                         validate_contract, validate_output)
from qavector_sdk.validation import SCHEMAS

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "plugin_contracts"


def example(name):
    return json.loads((EXAMPLES / (name + ".json")).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SCHEMAS)
def test_published_schema_is_valid(name):
    Draft202012Validator.check_schema(load_schema(name))


@pytest.mark.parametrize("name", [name for name in SCHEMAS if name != "plugin-common"])
def test_documented_examples_validate(name):
    document = example(name)
    assert validate_contract(name, document) is document


@pytest.mark.parametrize("name", [name for name in SCHEMAS if name != "plugin-common"])
def test_unknown_fields_are_never_silently_ignored(name):
    document = example(name)
    document["typo"] = "secret-not-for-errors"
    with pytest.raises(ContractError) as caught:
        validate_contract(name, document)
    assert caught.value.code == "CONTRACT_INVALID"
    assert "secret-not-for-errors" not in str(caught.value)


@pytest.mark.parametrize("name", [name for name in SCHEMAS if name != "plugin-common"])
def test_unsupported_wire_versions_are_rejected(name):
    document = example(name)
    key = "format_version" if name == "qavplug-manifest" else "schema_version"
    document[key] = 2 if key == "format_version" else "2.0.0"
    with pytest.raises(ContractError):
        validate_contract(name, document)


def test_loaded_schema_mutation_cannot_change_validation():
    load_schema("agent-profile")["additionalProperties"] = True
    profile = example("agent-profile")
    profile["api_key"] = "private"
    with pytest.raises(ContractError):
        validate_contract("agent-profile", profile)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "value"}, (1, 2), object()])
def test_non_json_data_rejected_even_with_unconstrained_schema(value):
    with pytest.raises(ContractError):
        validate_output(value, True)


def test_unknown_usage_is_null_not_zero():
    result = example("agent-result")
    validate_contract("agent-result", result)
    result["usage"] = 0
    with pytest.raises(ContractError):
        validate_contract("agent-result", result)
    result["usage"] = {"source": "provider", "metrics": [
        {"name": "input_tokens", "value": 0, "unit": "tokens"}]}
    validate_contract("agent-result", result)


def test_business_verdict_is_not_execution_failure():
    result = example("agent-result")
    assert result["status"] == "completed"
    assert result["output"]["verdict"] == "changes_required"
    validate_contract("agent-result", result)
    validate_output(result["output"], example("agent-request")["output_schema"])


def test_unknown_external_execution_cannot_advertise_automatic_retry():
    result = example("agent-result")
    result.update(status="unknown", error=example("agent-error"))
    validate_contract("agent-result", result)
    result["error"]["retryable"] = True
    with pytest.raises(ContractError):
        validate_contract("agent-result", result)


@pytest.mark.parametrize("status", ["pending", "running", "waiting_input", "cancelling"])
def test_terminal_result_rejects_nonterminal_state(status):
    result = example("agent-result")
    result["status"] = status
    with pytest.raises(ContractError):
        validate_contract("agent-result", result)


def test_completion_cannot_also_contain_failure():
    result = example("agent-result")
    result["error"] = example("agent-error")
    with pytest.raises(ContractError):
        validate_contract("agent-result", result)


@pytest.mark.parametrize("patch", [{"sequence": -1}, {"sequence": True},
                                   {"timestamp": "yesterday"}, {"payload": {}},
                                   {"timestamp": "2026-10-30T12:00:00+00:99"},
                                   {"timestamp": "2026-02-30T12:00:00Z"}])
def test_event_sequence_timestamp_and_payload_are_validated(patch):
    event = example("agent-event")
    event.update(patch)
    with pytest.raises(ContractError):
        validate_contract("agent-event", event)


def test_terminal_event_is_bound_to_its_attempt_and_status():
    event = example("agent-event")
    event.update(type="completed", payload={"result": example("agent-result")})
    validate_contract("agent-event", event)
    event["payload"]["result"]["attempt_id"] = "another-attempt"
    with pytest.raises(ContractError, match="attempt IDs"):
        validate_contract("agent-event", event)
    event["payload"]["result"]["attempt_id"] = event["attempt_id"]
    event["type"] = "failed"
    with pytest.raises(ContractError):
        validate_contract("agent-event", event)


def test_failed_event_can_report_unknown_remote_state():
    event = example("agent-event")
    result = example("agent-result")
    result.update(status="unknown", error=example("agent-error"))
    event.update(type="failed", payload={"result": result})
    validate_contract("agent-event", event)


def test_output_schema_mismatch_is_normalized_without_secret_values():
    with pytest.raises(ContractError) as caught:
        validate_output({"verdict": "secret-text"}, example("agent-request")["output_schema"])
    assert caught.value.code == "OUTPUT_SCHEMA_INVALID"
    assert caught.value.path == ("verdict",)
    assert "secret-text" not in str(caught.value)


@pytest.mark.parametrize("uri", ["https://example.invalid/schema", "file:///etc/passwd"])
def test_schema_references_never_fetch_network_or_files(uri, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Schema validation attempted network access")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(ContractError, match="schema set"):
        validate_output({}, {"$ref": uri})


def test_only_explicitly_supplied_schema_resources_can_resolve():
    schema = {"$ref": "urn:example:output"}
    resources = {"urn:example:output": {"type": "integer"}}
    validate_output(3, schema, local_schemas=resources)
    with pytest.raises(ContractError):
        validate_output("3", schema, local_schemas=resources)
    validate_output({"x": 3}, {"type": "object", "$defs": {"n": {"type": "integer"}},
                              "properties": {"x": {"$ref": "#/$defs/n"}}})


@pytest.mark.parametrize("schema", [False, {"type": "invalid"},
                                    {"$schema": "http://json-schema.org/draft-07/schema#"}])
def test_false_malformed_and_unsupported_schema_rejected(schema):
    with pytest.raises(ContractError):
        validate_output({}, schema)


def check_preflight(profile=None, request=None, caps=None, requires=None, config_schema=None):
    return preflight(profile or example("agent-profile"), request or example("agent-request"),
                     caps or example("agent-capabilities"), requires or example("agent-requirements"),
                     config_schema=config_schema or {"type": "object", "additionalProperties": False})


def test_supported_backend_passes_without_mutating_documents():
    profile, request, caps, requires = [example(name) for name in
        ("agent-profile", "agent-request", "agent-capabilities", "agent-requirements")]
    before = copy.deepcopy([profile, request, caps, requires])
    check_preflight(profile, request, caps, requires)
    assert [profile, request, caps, requires] == before


@pytest.mark.parametrize("requires,problem", [
    ({"workspace_access": "write"}, "workspace_access=write"),
    ({"session_resume": True}, "session_resume"),
    ({"interactive_input": True}, "interactive_input"),
    ({"tool_events": "full"}, "tool_events"),
    ({"tool_binding": ["mcp"]}, "tool_binding"),
    ({"content_types": ["image"]}, "content_types"),
    ({"hard_limits": ["tokens"]}, "hard limit: tokens"),
])
def test_incompatible_backend_is_rejected_before_execute(requires, problem):
    requires["schema_version"] = "1.0.0"
    with pytest.raises(ContractError, match=problem) as caught:
        check_preflight(requires=requires)
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"


@pytest.mark.parametrize("patch,problem", [
    ({"workspace_ref": None}, "workspace_ref"),
    ({"session_ref": "old-session"}, "session_resume"),
    ({"output_schema": {"type": "invalid"}}, "Invalid JSON Schema"),
])
def test_implicit_request_requirements_are_checked(patch, problem):
    request = example("agent-request")
    request.update(patch)
    with pytest.raises(ContractError, match=problem):
        check_preflight(request=request)


@pytest.mark.parametrize("limits", [{}, {"wall_time_seconds": 61}])
def test_request_cannot_remove_or_widen_profile_limits(limits):
    request = example("agent-request")
    request["limits"] = limits
    with pytest.raises(ContractError) as caught:
        check_preflight(request=request)
    assert caught.value.code == "POLICY_UNSATISFIED"


def test_unsupported_structured_output_blocks_execution():
    caps = example("agent-capabilities")
    caps["structured_output"] = "unsupported"
    with pytest.raises(ContractError, match="structured_output"):
        check_preflight(caps=caps)


def test_requesting_cancellation_does_not_mean_confirmed_stop():
    caps = example("agent-capabilities")
    caps["cancellation"] = "request"
    with pytest.raises(ContractError, match="confirmed_cancellation"):
        check_preflight(caps=caps, requires={"schema_version": "1.0.0", "confirmed_cancellation": True})


@pytest.mark.parametrize("patch,problem", [({"workspace_access": "none"}, "workspace_access"),
                                          ({"content_types": []}, "text task input")])
def test_request_cannot_use_unsupported_implicit_capability(patch, problem):
    caps = example("agent-capabilities")
    caps.update(patch)
    with pytest.raises(ContractError, match=problem):
        check_preflight(caps=caps, requires={"schema_version": "1.0.0"})


def test_profile_options_cannot_enable_capabilities():
    profile = example("agent-profile")
    profile["backend_options"] = {"session_resume": True}
    with pytest.raises(ContractError) as caught:
        check_preflight(profile=profile)
    assert caught.value.code == "CONFIG_INVALID"


def test_backend_schema_must_explicitly_reject_unknown_options():
    with pytest.raises(ContractError, match="closed object"):
        check_preflight(config_schema={"type": "object"})


@pytest.mark.parametrize("patch", [
    {"kind": "service_provider"}, {"entrypoint": "module:run()"},
    {"config_schema": "../outside.json"}, {"config_schema": "/etc/passwd"},
    {"config_schema": "schemas/../outside.json"},
    {"config_schema": "C:\\outside.json"}, {"kind": "node_provider"},
])
def test_manifest_rejects_unsupported_contributions_and_invalid_references(patch):
    manifest = example("qavplug-manifest")
    manifest["contributions"][0].update(patch)
    with pytest.raises(ContractError):
        validate_contract("qavplug-manifest", manifest)


def test_node_provider_requires_input_and_output_schemas():
    manifest = example("qavplug-manifest")
    manifest["contributions"][0].update(kind="node_provider", input_schema="schemas/input.json",
                                        output_schema="schemas/output.json")
    del manifest["compatibility"]["agent_api"]
    validate_contract("qavplug-manifest", manifest)


def test_agent_requires_agent_api_compatibility():
    manifest = example("qavplug-manifest")
    del manifest["compatibility"]["agent_api"]
    with pytest.raises(ContractError):
        validate_contract("qavplug-manifest", manifest)


def test_duplicate_contribution_ids_are_rejected_even_with_different_entrypoints():
    manifest = example("qavplug-manifest")
    other = dict(manifest["contributions"][0], entrypoint="other:Backend")
    manifest["contributions"].append(other)
    with pytest.raises(ContractError, match="Duplicate contribution"):
        validate_contract("qavplug-manifest", manifest)


def test_interplugin_dependencies_are_not_silently_accepted_in_p0():
    manifest = example("qavplug-manifest")
    manifest["dependencies"]["plugins"] = ["example.other"]
    with pytest.raises(ContractError):
        validate_contract("qavplug-manifest", manifest)


def test_sdk_import_does_not_load_private_core_gui_or_backends():
    subprocess.run([sys.executable, "-c", "import sys; import qavector_sdk; "
                    "assert not any(n.split('.')[0] in "
                    "{'cycle', 'execution_runtime', 'cms_gui', 'PySide6', 'crewai', 'autogen'} "
                    "for n in sys.modules)"], check=True)


def test_ordinary_plugin_runs_without_an_agent_profile_or_model():
    from examples.plugin_contracts.count_provider import CountProvider

    request = example("node-request")
    validate_contract("node-request", request)
    context = example("execution-context")
    validate_contract("execution-context", context)
    manifest = example("node-manifest")
    validate_contract("qavplug-manifest", manifest)
    contribution = manifest["contributions"][0]
    for field, value in (("config_schema", request["config"]), ("input_schema", request["inputs"])):
        schema = json.loads((EXAMPLES / contribution[field]).read_text())
        validate_output(value, schema)
    events = []

    async def emit(event):
        validate_contract("node-event", event)
        events.append(event)

    result = asyncio.run(CountProvider().execute(request, context, emit))
    validate_contract("node-result", result)
    validate_output(result["output"], request["output_schema"])
    assert result["output"] == {"count": 2}
    assert events[0]["attempt_id"] == request["attempt_id"]
    assert "backend_ref" not in result
    assert "agent_api" not in manifest["compatibility"]


def test_one_package_can_contribute_agents_and_ordinary_nodes():
    manifest = example("qavplug-manifest")
    manifest["contributions"] += example("node-manifest")["contributions"]
    validate_contract("qavplug-manifest", manifest)


def test_node_terminal_event_cannot_impersonate_another_attempt():
    event = example("node-event")
    event.update(type="completed", payload={"result": example("node-result")})
    validate_contract("node-event", event)
    event["attempt_id"] = "other-attempt"
    with pytest.raises(ContractError, match="attempt IDs"):
        validate_contract("node-event", event)


def test_schema_metavalidation_cannot_fetch_missing_standard_resources(monkeypatch):
    import jsonschema.validators
    from referencing import Registry
    from qavector_sdk import validate_schema
    monkeypatch.setattr(jsonschema.validators, "SPECIFICATIONS", Registry())
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: pytest.fail("No remote metaschema fetch"))
    with pytest.raises(ContractError):
        validate_schema({"type": "string"})
