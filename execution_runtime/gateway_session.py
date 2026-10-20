"""One native invocation's provider capability. No native descriptors in this contract."""

import base64
import hashlib
import http.client
import json
import math
import threading
import time
import uuid
from urllib.parse import urlsplit

from ._json import canonical, object_fields, parse_document
from .errors import RuntimePolicyError
from .gateway import ProviderGateway, STREAM_CONTRACT, _PinnedHTTPS
from .secrets import SecretValues


class StreamRedactor:
    def __init__(self, values):
        self.patterns = sorted({part.encode() for value in values for part in
            (value, json.dumps(value, ensure_ascii=True)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]) if part}, key=len, reverse=True)
        self.pending = b""

    def feed(self, block, final=False):
        self.pending += block
        cutoff = len(self.pending) if final else max(0, len(self.pending) - max(map(len, self.patterns), default=1) + 1)
        cursor, chunks = 0, []
        while cursor < cutoff:
            matches = [(self.pending.find(value, cursor), value) for value in self.patterns]
            matches = [(start, value) for start, value in matches if 0 <= start < cutoff]
            if not matches:
                chunks.append(self.pending[cursor:cutoff]); cursor = cutoff
                break
            start, value = min(matches, key=lambda one: (one[0], -len(one[1])))
            chunks.extend((self.pending[cursor:start], b"[redacted]"))
            cursor = start + len(value)
        self.pending = self.pending[cursor:]
        return b"".join(chunks)


class GatewaySession:
    """A nonserializable, one-owner channel bound to an approved operation policy."""
    def __init__(self, route, policy, model, values, deadline=None):
        if not isinstance(route, ProviderGateway) or route.to_document()["contract"] != STREAM_CONTRACT:
            raise RuntimePolicyError("gateway_stream_contract_required", "The native CLI requires provider-egress/v2.")
        route.authorize(policy, model)
        if not isinstance(values, SecretValues) or set(values.names) != set(policy.policy.to_document()["secrets"]):
            raise RuntimePolicyError("gateway_credential_unavailable", "Supply the exact scoped OAuth credential.")
        if deadline is not None:
            try:
                valid = type(deadline) in (int, float) and math.isfinite(deadline)
            except OverflowError:
                valid = False
            if not valid:
                raise RuntimePolicyError("invalid_gateway_deadline", "The gateway deadline must be a finite monotonic timestamp.")
            if deadline <= time.monotonic():
                raise RuntimePolicyError("gateway_timeout", "The provider channel deadline expired before preparation.")
        self.route, self.policy, self.model = route, policy, model
        self._values, self.deadline = values, deadline
        self.capability = "sk-ant-oat01-runtime-" + uuid.uuid4().hex
        self._stop = threading.Event()
        self._connection, self._bound, self._thread = None, False, None
        self.calls, self.failed, self.closed, self._seen = 0, False, False, set()
        self._lock = threading.Lock()

    def bind(self):
        with self._lock:
            if self._bound or self.closed:
                raise RuntimePolicyError("gateway_session_consumed", "A gateway session belongs to exactly one native attempt.")
            self._bound = True

    def raise_if_set(self):
        if self._stop.is_set():
            raise RuntimePolicyError("gateway_cancelled", "The provider channel was stopped.")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RuntimePolicyError("gateway_timeout", "The provider channel deadline expired.")

    def close(self):
        self._stop.set()
        if self._connection is not None:
            self._connection.abort()
        self.closed = True

    def summary(self):
        return dict(self.route.summary(), execution_status="native_bridge", calls=self.calls,
                    failed=self.failed, closed=self.closed, workload_network="private_loopback_tcp",
                    credential_delivery="core_oauth_header")

    def release(self):
        self.close()
        if self._thread is not None and self._thread.is_alive():
            raise RuntimePolicyError("gateway_cleanup_unconfirmed", "The provider transport still owns invocation resources.")
        self._values = SecretValues()

    def values_for_redaction(self):
        return tuple(value for _, value in self._values.items()) + (self.capability,)

    def handle(self, packet, send):
        """Validate one data frame and emit bounded response frames through the native writer."""
        object_fields(packet, ("kind", "id", "path", "headers", "body"), "gateway.frame",
                      ("kind", "id", "path", "headers", "body"))
        if packet["kind"] != "request" or type(packet["id"]) is not int or packet["id"] != self.calls + 1:
            raise RuntimePolicyError("invalid_gateway_frame", "Gateway frames must use sequential request identifiers.")
        config = self.route.to_document()
        if self.failed or self.closed or self.calls >= config.get("max_requests", 4):
            raise RuntimePolicyError("gateway_budget_exhausted", "This invocation's provider request capability is exhausted.")
        headers = packet["headers"]
        if (packet["path"] != urlsplit(config["endpoint"]).path or not isinstance(headers, dict) or
                headers.get("authorization") != "Bearer " + self.capability or
                set(headers) - {"authorization", "anthropic-version", "anthropic-beta", "x-app", "user-agent"} or
                any(not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value)
                    for value in headers.values())):
            raise RuntimePolicyError("gateway_request_denied", "The native request does not match its endpoint/header capability.")
        try:
            if not isinstance(packet["body"], str) or len(packet["body"]) > 1400000:
                raise ValueError()
            body = base64.b64decode(packet["body"], validate=True)
        except (ValueError, TypeError):
            raise RuntimePolicyError("invalid_gateway_body", "Expected a bounded encoded request body.") from None
        if len(body) > config.get("request_bytes", 524288):
            raise RuntimePolicyError("gateway_request_too_large", "Provider request exceeds its approved size.")
        data = parse_document(body)
        if not isinstance(data, dict) or data.get("model") != self.model:
            raise RuntimePolicyError("gateway_model_changed", "The CLI request must use the approved full model name.")
        signature = hashlib.sha256(canonical(data).encode()).hexdigest()
        if signature in self._seen:
            raise RuntimePolicyError("gateway_duplicate_request", "An identical provider request cannot be sent twice in this invocation.")
        self.route.authorize(self.policy, self.model)
        self.raise_if_set()
        self._seen.add(signature)
        self.calls += 1
        try:
            self._stream(body, headers, lambda frame: send(dict(frame, id=packet["id"])))
        except BaseException:
            self.failed = True
            raise

    def _stream(self, body, incoming, send):
        config = self.route.to_document()
        until = min(self.deadline if self.deadline is not None else float("inf"), time.monotonic() + config.get("timeout_seconds", 30))
        if until <= time.monotonic():
            self.raise_if_set()
        headers = {name: value for name, value in incoming.items() if name != "authorization"}
        headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Connection": "close"})
        value = dict(self._values.items())[config["credential"]]
        if not value or len(value) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise RuntimePolicyError("invalid_gateway_credential", "The scoped OAuth credential is not a valid bearer token.")
        headers["Authorization"] = "Bearer " + value
        connection = self._connection = _PinnedHTTPS(self.route, max(0.001, until - time.monotonic()))
        done, errors = threading.Event(), []
        def work():
            try:
                self.raise_if_set()
                connection.request("POST", urlsplit(config["endpoint"]).path, body=body, headers=headers)
                response = connection.getresponse()
                content = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                if response.status != 200 or content not in ("application/json", "text/event-stream") or response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise RuntimePolicyError("gateway_response_denied", "Expected HTTP 200 uncompressed JSON/SSE; redirects and retries are disabled.")
                send({"kind": "headers", "content_type": content})
                total, redactor = 0, StreamRedactor(self.values_for_redaction())
                while True:
                    self.raise_if_set()
                    block = response.read1(16384)
                    if not block:
                        break
                    total += len(block)
                    if total > config.get("response_bytes", 65536):
                        raise RuntimePolicyError("gateway_response_too_large", "The provider stream exceeded its approved bound.")
                    safe = redactor.feed(block)
                    if safe:
                        send({"kind": "data", "body": base64.b64encode(safe).decode()})
                safe = redactor.feed(b"", final=True)
                if safe:
                    send({"kind": "data", "body": base64.b64encode(safe).decode()})
                send({"kind": "end"})
            except RuntimePolicyError as error:
                errors.append(error)
            except (OSError, ValueError, http.client.HTTPException):
                errors.append(RuntimePolicyError("gateway_transport_failed", "The provider request outcome may be uncertain; automatic retry is blocked."))
            finally:
                connection.abort()
                connection.close()
                done.set()
        thread = self._thread = threading.Thread(target=work, name="qavector-cli-egress", daemon=True)
        thread.start()
        try:
            while not done.wait(0.02):
                self.raise_if_set()
                if time.monotonic() >= until:
                    raise RuntimePolicyError("gateway_timeout", "The provider request deadline expired.")
            self.raise_if_set()
            if time.monotonic() >= until:
                raise RuntimePolicyError("gateway_timeout", "The provider request deadline expired.")
            if errors:
                raise errors[0]
        finally:
            connection.abort()
            thread.join(2)
            self._connection = None
            if thread.is_alive():
                raise RuntimePolicyError("gateway_cleanup_unconfirmed", "The provider transport did not stop.")
