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

from .contracts import WRITE_ERRORS, WRITE_SAYS, ReadError, WriteError


def builtin_providers():
    """Every provider this build knows: ``integrations.<name>.capabilities.provider()``."""
    import importlib
    from . import PROVIDERS
    return {name: importlib.import_module("integrations.%s.capabilities" % name).provider() for name in PROVIDERS}


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
                        for op, contract in provider.contracts.items() if contract.access in ("read", "write")}

    def connections(self):
        from . import store
        return [{key: entry[key] for key in ("id", "provider", "name")}
                for entry in store.load(self._path)]

    def access(self, connection_id, provider_name, kind):
        """What ``kind`` of caller ("agents", "cycles") may do through a connection,
        as the connection says (ADR-015 D5) - read fresh each time - or None
        where its provider sets no such limit (Jira, for now). A connection
        that is gone allows nothing."""
        from . import store
        module = store._provider(provider_name)
        rule = getattr(module, "access_of", None) if module is not None else None
        if rule is None:
            return None
        entry = next((one for one in store.load(self._path)
                      if one["id"] == connection_id and store.provider_of(one) == provider_name), None)
        return rule(entry, kind) if entry is not None else "off"

    def ceilings(self, kind):
        """``{(provider, connection id): level}`` for every connection whose
        provider sets a limit for ``kind`` of caller."""
        from . import store
        found = {}
        for entry in store.load(self._path):
            provider = store.provider_of(entry)
            module = store._provider(provider)
            rule = getattr(module, "access_of", None) if module is not None else None
            if rule is not None:
                found[(provider, entry["id"])] = rule(entry, kind)
        return found

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
            result = _redact(result, _secrets_of(connection, token))
            check_output(result)
        except Exception:
            raise ReadError("invalid_response") from None
        return result

    # -- writes (QCR ADR-007): prepared, shown to a person, then made ------------------
    def _writable(self, provider_name, operation, request):
        provider = self.providers.get(provider_name)
        contract = provider.contracts.get(operation) if provider is not None else None
        if contract is None or contract.access != "write":
            raise WriteError("unsupported")
        check_input, check_output = self._checks[(provider_name, operation)]
        try:
            check_input(request)
        except Exception:
            raise WriteError("invalid_request") from None
        return provider, check_output

    def _connection(self, connection_id, provider_name):
        from . import store
        entry = store.get(connection_id, self._path)
        if entry is None or entry["provider"] != provider_name:
            raise WriteError("unavailable")
        connection = store.credentials(connection_id, self._path, self._secrets, self._environments)
        if connection["provider"] != provider_name:
            raise WriteError("unavailable")
        return connection

    def prepare(self, connection_id, provider_name, operation, request):
        """What a person decides a write on: ``{request, card, editable, noop}`` -
        ``editable`` being the request fields the card lets the person change.
        Asks the service (a read); changes nothing. Raises :class:`WriteError`."""
        provider, check_output = self._writable(provider_name, operation, request)
        try:
            connection = self._connection(connection_id, provider_name)
            token = connection["token"]
            made = provider.prepare(operation, connection, token, copy.deepcopy(request))
        except Exception as exc:
            raise _write_error(exc) from None
        try:
            made = _redact(made, _secrets_of(connection, token))
            if made.get("noop") is not None:
                check_output(made["noop"])
            elif made.get("request") is not None:
                self._checks[(provider_name, operation)][0](made["request"])
            else:
                raise ValueError("Nothing to make.")
        except Exception:
            raise WriteError("invalid_response") from None
        card = made.get("card") or {}
        return {"request": made.get("request"), "card": card,
                "editable": list((card.get("edit") or {}).keys()), "noop": made.get("noop")}

    def write(self, connection_id, provider_name, operation, request, source=None):
        """Make one approved write; the result its contract promises. Made or
        failed, it is a line in the activity log under ``source`` - without its
        text. Raises :class:`WriteError`."""
        from . import activity
        provider, check_output = self._writable(provider_name, operation, request)
        target = str(request.get("key") or request.get("project") or "")
        caller, source = source, {key: value for key, value in (source or {}).items()
                                  if key not in ("actor", "approved_by")} or None
        try:
            connection = self._connection(connection_id, provider_name)
            token = connection["token"]
        except Exception as exc:
            raise _write_error(exc) from None
        try:
            # Who asked, and who approved, go with it: a provider that keeps a
            # record of what it sent (Teams, ADR-015) writes them down.
            result = provider.write(operation, dict(connection, caller=caller), token, copy.deepcopy(request))
        except Exception as exc:
            error = _write_error(exc)
            # Refused before anything was changed (fields asked for, changed
            # meanwhile): nothing to log, as for the page's own writes.
            if error.code not in WRITE_SAYS:
                activity.record(self._path, connection_id, operation, target, False, WRITE_ERRORS[error.code],
                                source, provider_name)
            raise error from None
        try:
            result = _redact(result, _secrets_of(connection, token))
            check_output(result)
        except Exception:
            # Made, but its answer is unusable: say what is known.
            activity.record(self._path, connection_id, operation, target, True, operation, source, provider_name)
            raise WriteError("invalid_response") from None
        made = (result.get("issue") or {}).get("key") or target      # a new issue's own key
        activity.record(self._path, connection_id, operation, made, True,
                        str(result.get("summary") or operation)[:200], source, provider_name)
        return result


def _secrets_of(connection, token):
    basic = base64.b64encode((connection.get("email", "") + ":" + token).encode()).decode()
    return [token, basic, connection.get("api"), connection.get("webhook"), connection.get("webhook_url")]


def _write_error(exc):
    if isinstance(exc, WriteError):
        return exc
    return WriteError(_code_of(exc))


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
