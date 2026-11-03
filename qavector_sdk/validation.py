"""Strict JSON validation with a closed, in-memory schema registry.

No URI can trigger network or filesystem reads. Validation messages deliberately
omit instance values and schema text, which may contain credentials or prompts.
"""

import json
import re
from datetime import datetime
from functools import lru_cache
from importlib import resources

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable
from referencing.jsonschema import DRAFT202012

DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMAS = ("plugin-common", "plugin-error", "agent-profile", "agent-request", "agent-event",
           "agent-result", "agent-error", "agent-capabilities",
           "agent-requirements", "execution-context", "qavplug-manifest",
           "plugin-contribution", "node-request", "node-result", "node-event",
           "plugin-initialize", "plugin-execute", "plugin-host-call",
           "plugin-input-response", "plugin-rpc")
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _date_time(value):
    # jsonschema's optional RFC3339 dependency may be absent. Always enforce
    # our timestamp subset (seconds 00-59, explicit offset, no leap seconds).
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value):
        return False
    datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    return True


class ContractError(ValueError):
    def __init__(self, code, message, path=()):
        self.code = code
        self.path = tuple(path)
        super().__init__(message)


def load_schema(name):
    """Return a fresh schema so callers cannot mutate subsequent validation."""
    if name not in SCHEMAS:
        raise ValueError("Unknown contract schema")
    return json.loads(resources.files("qavector_sdk").joinpath("schemas").joinpath(
        name + "-v1.schema.json").read_text(encoding="utf-8"))


def _deny_retrieval(uri):
    raise NoSuchResource(ref=uri)


def _resource(schema, code):
    if isinstance(schema, dict) and schema.get("$schema", DIALECT) != DIALECT:
        raise ContractError(code, "Only JSON Schema Draft 2020-12 is supported")
    try:
        # check_schema() uses jsonschema's default remote-retrieving registry.
        # Even validating a schema must fail closed if bundled metaschema
        # resources are missing (e.g. an incomplete frozen worker image).
        meta = Draft202012Validator(Draft202012Validator.META_SCHEMA,
                                   registry=Registry(retrieve=_deny_retrieval))
        if next(meta.iter_errors(schema), None) is not None:
            raise ContractError(code, "Invalid JSON Schema")
        return Resource.from_contents(schema, default_specification=DRAFT202012)
    except Unresolvable:
        raise ContractError(code, "Required local JSON Schema resources are missing") from None
    except (SchemaError, TypeError, ValueError):
        raise ContractError(code, "Invalid JSON Schema") from None


def validate_schema(schema, *, code="OUTPUT_SCHEMA_INVALID"):
    """Check a Draft 2020-12 schema without resolving external references."""
    _json_document(schema, code)
    _resource(schema, code)
    return schema


def _json_document(value, code):
    # The wire contract excludes Python objects, non-string keys and NaN/Inf.
    # json.dumps alone coerces keys and tuples, hiding invalid wire data.
    def check(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ContractError(code, "JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ContractError(code, "Value is not JSON data")
    try:
        check(value)
        json.dumps(value, allow_nan=False)
    except ContractError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ContractError(code, "Value is not finite JSON data") from None


def _validate(value, schema, registry, code):
    _json_document(value, code)
    try:
        error = next(Draft202012Validator(
            schema, registry=registry, format_checker=FORMAT_CHECKER
        ).iter_errors(value), None)
    except Unresolvable:
        raise ContractError(code, "Schema reference is outside the supplied schema set or missing") from None
    if error is not None:
        # Do not interpolate jsonschema's message: it includes rejected values.
        raise ContractError(code, "Contract validation failed (%s)" % error.validator,
                            error.absolute_path)
    return value


@lru_cache(maxsize=1)
def _bundled_registry():
    registry = Registry(retrieve=_deny_retrieval)
    for contract in SCHEMAS:
        document = load_schema(contract)
        registry = registry.with_resource(document["$id"], _resource(document, "CONTRACT_INVALID"))
    return registry


def validate_contract(name, value):
    """Validate wire data only; returns the original document on success."""
    _validate(value, load_schema(name), _bundled_registry(), "CONTRACT_INVALID")
    if name in ("agent-event", "node-event") and "result" in value["payload"]:
        if value["attempt_id"] != value["payload"]["result"]["attempt_id"]:
            raise ContractError("CONTRACT_INVALID", "Event and result attempt IDs differ")
    if name == "qavplug-manifest":
        ids = [item["id"] for item in value["contributions"]]
        if len(ids) != len(set(ids)):
            raise ContractError("CONTRACT_INVALID", "Duplicate contribution ID")
    return value


def validate_output(value, schema, *, local_schemas=None, code="OUTPUT_SCHEMA_INVALID"):
    """Validate output/options using explicitly supplied schemas only.

    The caller supplies an already bounded, approved local schema set. Registry
    keys are schema resource IDs, not paths to open. No resolver fetches files.
    """
    validate_schema(schema, code=code)
    registry = Registry(retrieve=_deny_retrieval)
    for identifier, document in (local_schemas or {}).items():
        _json_document(document, code)
        registry = registry.with_resource(identifier, _resource(document, code))
    return _validate(value, schema, registry, code)
