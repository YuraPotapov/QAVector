"""Reuse SDK validation, with eager resolution of registered schema references."""

import copy
import json

from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable
from referencing.jsonschema import DRAFT202012
from qavector_sdk.validation import ContractError, DIALECT, compile_output, validate_schema

from .errors import QCRError


def _deny(uri):
    raise NoSuchResource(ref=uri)


class Schemas:
    def __init__(self, local_schemas=None):
        self._local = copy.deepcopy(local_schemas or {})
        for schema in self._local.values():
            self.check(schema)

    def check(self, schema):
        try:
            validate_schema(schema)
            registry = Registry(retrieve=_deny)
            for identifier, local in self._local.items():
                validate_schema(local)
                registry = registry.with_resource(identifier, Resource.from_contents(
                    local, default_specification=DRAFT202012))
            root = Resource.from_contents(schema, default_specification=DRAFT202012)
            resolver = registry.resolver_with_root(root)

            def walk(resource, current):
                value = resource.contents
                if isinstance(value, dict):
                    if value.get("$schema", DIALECT) != DIALECT:
                        raise QCRError("invalid_schema", "Unsupported schema dialect.")
                    for key in ("$ref", "$dynamicRef"):
                        if key in value:
                            current.lookup(value[key])
                for child in resource.subresources():
                    walk(child, current.in_subresource(child))

            walk(root, resolver)
        except (ContractError, Unresolvable, ValueError, TypeError, RecursionError):
            raise QCRError("invalid_schema", "Schema is invalid or references an unregistered schema.") from None

    def validate(self, value, schema, *, code="invalid_property"):
        try:
            self._compiled(schema)(value)
        except (ContractError, RecursionError, TypeError, ValueError):
            raise QCRError(code, "Value does not match its registered schema.") from None

    #: Compiled schemas kept; registered contracts are a bounded set, so this is
    #: never reached in practice and only stops an unbounded growth.
    _KEEP = 512

    def _compiled(self, schema):
        """The schema checked once against the metaschema, then reused: checking
        it again on every read cost ~45 ms, validating a value well under one."""
        cache = self.__dict__.setdefault("_cache", {})
        key = json.dumps(schema, sort_keys=True)
        check = cache.get(key)
        if check is None:
            if len(cache) >= self._KEEP:
                cache.clear()
            check = cache[key] = compile_output(schema, local_schemas=self._local)
        return check
