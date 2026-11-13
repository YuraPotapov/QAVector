"""Lazy project-bound provider reads. Secrets never leave this core boundary.

This module does not authorize actors: its caller must do that before read().
Connections/property discovery only reads configuration, never credentials or
remote APIs. Existing credential and provider executors remain authoritative.
"""

import base64
import copy
import html
import json
import os
from urllib.parse import quote, quote_plus

from qavector_sdk.validation import compile_output

from .contracts import ReadError


def builtin_providers():
    from .jira.capabilities import provider
    return {"jira": provider()}


def _redact(value, secrets):
    """Remove known credentials/endpoints, including common encoded echoes."""
    variants = set()
    for secret in secrets:
        if not isinstance(secret, str) or not secret:
            continue
        variants.update((secret, quote(secret, safe=""), quote_plus(secret), html.escape(secret),
                         json.dumps(secret, ensure_ascii=True)[1:-1]))
    variants = sorted(variants, key=len, reverse=True)

    def clean(item):
        if isinstance(item, str):
            for secret in variants:
                item = item.replace(secret, "[redacted]")
            return item
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, dict):
            return {clean(key): clean(child) for key, child in item.items()}
        return item
    return clean(value)


class ProjectReader:
    def __init__(self, integrations_path, secrets_path, environments_path, *, providers=None):
        if any(not path for path in (integrations_path, secrets_path, environments_path)):
            raise ValueError("Project paths must be explicit.")
        self._path, self._secrets, self._environments = (
            os.path.abspath(path) for path in (integrations_path, secrets_path, environments_path))
        self.providers = copy.deepcopy(builtin_providers() if providers is None else providers)
        # Each contract's schemas checked once, not on every read (see compile_output).
        self._checks = {(name, op): (compile_output(contract.input_schema), compile_output(contract.output_schema))
                        for name, provider in self.providers.items()
                        for op, contract in provider.contracts.items() if contract.access == "read"}

    def connections(self):
        from . import store
        return [{key: entry[key] for key in ("id", "provider", "name")}
                for entry in store.load(self._path)]

    def read(self, connection_id, provider_name, operation, request):
        """One provider read. Raises :class:`ReadError` with a code and nothing else.

        The site's address is not redacted: it is no secret (the connections
        file shows it) and every issue link in a result is built from it. The
        token, its basic-auth form, the gateway address and a webhook are.
        """
        from . import store
        provider = self.providers.get(provider_name)
        contract = provider.contracts.get(operation) if provider is not None else None
        if contract is None or contract.access != "read":
            raise ReadError("unsupported")
        check_input, check_output = self._checks[(provider_name, operation)]
        try:
            check_input(request)
        except Exception:
            raise ReadError("invalid_request") from None
        try:
            # Check live provider identity before credentials (which may refresh OAuth).
            entry = store.get(connection_id, self._path)
            if entry is None or entry["provider"] != provider_name:
                raise ReadError("unavailable")
            connection = store.credentials(connection_id, self._path, self._secrets, self._environments)
            if connection["provider"] != provider_name:
                raise ReadError("unavailable")
            token = connection["token"]
            result = provider.read(operation, connection, token, copy.deepcopy(request))
        except Exception as exc:
            raise ReadError(_code_of(exc)) from None
        try:
            check_output(result)
            basic = base64.b64encode((connection.get("email", "") + ":" + token).encode()).decode()
            result = _redact(result, [token, basic, connection.get("api"),
                                      connection.get("webhook"), connection.get("webhook_url")])
            check_output(result)
        except Exception:
            raise ReadError("invalid_response") from None
        return result


#: What a refused connection's ``store.Unavailable.kind`` means to a caller.
_UNAVAILABLE = {"not_found": "unavailable", "no_token": "no_token", "scope": "scope_invalid",
                "signin": "signin", "app": "signin"}


def _code_of(exc):
    """The read error code for what a provider read raised.

    Only the exception's type and its numeric/enumerated attributes are looked
    at, never its message. A provider error carries ``status`` - the HTTP status,
    0 for "could not reach it" - as ``JiraError`` does.
    """
    from . import store
    if isinstance(exc, ReadError):
        return exc.code
    if isinstance(exc, store.Unavailable):
        return _UNAVAILABLE.get(exc.kind, "unavailable")
    status = getattr(exc, "status", None)
    if type(status) is int:
        if status == 0:
            return "network"
        if status in (401, 403):
            return "auth"
        if status == 404:
            return "not_found"
        if status == 429:
            return "rate_limited"
        if 400 <= status < 500:
            return "invalid_request"
        return "service_error"
    if isinstance(exc, (OSError, TimeoutError)):
        return "network"
    return "failed"
