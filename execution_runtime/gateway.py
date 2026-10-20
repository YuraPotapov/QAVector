"""Provider egress foundation. Native CLI bridging is a separate runtime adapter."""

import http.client
import ipaddress
import math
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from ._json import Snapshot, object_fields, parse_document
from .errors import RuntimePolicyError
from .policy import PolicyPatch
from .secrets import SecretValues

CONTRACT = "qavector.provider-egress/v1"
STREAM_CONTRACT = "qavector.provider-egress/v2"
LIMIT = 65536


class ProviderGateway(Snapshot):
    def __init__(self, document):
        object_fields(document, ("contract", "provider", "endpoint", "addresses", "models", "timeout_seconds",
                                "response_bytes", "allow_private_addresses", "ca_pem", "credential",
                                "authentication", "request_bytes", "max_requests"), "gateway",
                      ("contract", "provider", "endpoint", "addresses", "models"))
        streaming = document["contract"] == STREAM_CONTRACT
        if document["contract"] not in (CONTRACT, STREAM_CONTRACT):
            raise RuntimePolicyError("unsupported_gateway_contract", "Expected the versioned provider egress contract.")
        if document["provider"] != "claude_cli":
            raise RuntimePolicyError("unsupported_gateway_provider", "This gateway profile supports the Claude Code CLI provider.")
        if streaming:
            if document.get("authentication") != "claude-oauth-token" or not document.get("credential"):
                raise RuntimePolicyError("invalid_gateway_authentication", "Claude streaming requires a scoped subscription OAuth token reference.")
        elif any(key in document for key in ("authentication", "request_bytes", "max_requests")):
            raise RuntimePolicyError("unsupported_gateway_contract", "Streaming options require provider-egress/v2.")
        endpoint = document["endpoint"]
        try:
            if (not isinstance(endpoint, str) or len(endpoint) > 2048 or
                    any(ord(char) < 33 or ord(char) > 126 for char in endpoint) or "\\" in endpoint):
                raise ValueError()
            url = urlsplit(endpoint)
            host, port = url.hostname, 443 if url.port is None else url.port
            if (url.scheme != "https" or not host or url.username is not None or url.password is not None or
                    "?" in endpoint or "#" in endpoint or not url.path.startswith("/") or
                    url.path.startswith("//") or url.netloc.endswith(":")):
                raise ValueError()
            PolicyPatch({"network": {"mode": "restricted", "endpoints": [{"host": host, "port": port}]}})
            if streaming and url.path != "/v1/messages":
                raise ValueError()
        except (ValueError, RuntimePolicyError):
            raise RuntimePolicyError("invalid_gateway_endpoint", "Choose an exact HTTPS endpoint without user info, query or fragment.") from None
        private = document.get("allow_private_addresses", False)
        addresses = document["addresses"]
        if type(private) is not bool or not isinstance(addresses, list) or not 1 <= len(addresses) <= 8:
            raise RuntimePolicyError("invalid_gateway_addresses", "Pin 1..8 reviewed IP addresses; private addresses need explicit approval.")
        seen = set()
        for value in addresses:
            try:
                if not isinstance(value, str) or "%" in value:
                    raise ValueError()
                address = ipaddress.ip_address(value)
                if (value != str(address) or value in seen or address.is_unspecified or address.is_multicast or
                        address.is_link_local or address.is_reserved or getattr(address, "ipv4_mapped", None) is not None or
                        not address.is_global and not (private and (address.is_private or address.is_loopback))):
                    raise ValueError()
            except ValueError:
                raise RuntimePolicyError("invalid_gateway_address", "Gateway IP pins must be unambiguous approved unicast addresses; metadata/link-local addresses are forbidden.") from None
            seen.add(value)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and seen != {str(literal)}:
            raise RuntimePolicyError("gateway_address_mismatch", "An IP endpoint must use the same IP pin.")
        models = document["models"]
        if (not isinstance(models, list) or not 1 <= len(models) <= 32 or
                any(not isinstance(one, str) or not one or len(one) > 256 or
                    any(ord(c) < 33 or ord(c) > 126 for c in one) for one in models) or len(set(models)) != len(models)):
            raise RuntimePolicyError("invalid_gateway_models", "Approve an exact nonempty model allowlist.")
        timeout, bound = document.get("timeout_seconds", 30), document.get("response_bytes", LIMIT)
        try:
            valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 120
        except OverflowError:
            valid_timeout = False
        if not valid_timeout or type(bound) is not int or not 1 <= bound <= (16 * LIMIT if streaming else LIMIT):
            raise RuntimePolicyError("invalid_gateway_limits", "Bound gateway timeout to 120 seconds; response is limited to 64 KiB for v1 and 1 MiB for v2.")
        for key, default, maximum in (("request_bytes", 8 * LIMIT, 8 * LIMIT), ("max_requests", 4, 16)):
            number = document.get(key, default)
            if type(number) is not int or not 1 <= number <= maximum:
                raise RuntimePolicyError("invalid_gateway_limits", "Gateway request size/budget exceeds its bounded contract.")
        ca = document.get("ca_pem")
        if ca is not None and (not isinstance(ca, str) or not ca or len(ca) > 16384):
            raise RuntimePolicyError("invalid_gateway_ca", "The optional reviewed CA must be bounded PEM text.")
        if ca is not None:
            try:
                ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=ca)
            except (OSError, ValueError):
                raise RuntimePolicyError("invalid_gateway_ca", "The reviewed CA is not a valid PEM certificate.") from None
        if "credential" in document:
            PolicyPatch({"secrets": [document["credential"]]})
        super().__init__(document)

    @property
    def endpoint(self):
        url = urlsplit(self.to_document()["endpoint"])
        return {"host": url.hostname, "port": 443 if url.port is None else url.port, "protocol": "tcp"}

    def authorize(self, policy, model=None):
        raw = policy.policy.to_document()
        if raw["network"]["mode"] != "restricted" or self.endpoint not in raw["network"]["endpoints"]:
            raise RuntimePolicyError("gateway_network_denied", "The node must grant this approved gateway endpoint through restricted network policy.")
        if model is not None and model not in self.to_document()["models"]:
            raise RuntimePolicyError("gateway_model_denied", "This model is not approved for the gateway.")
        reference = self.to_document().get("credential")
        if set(raw["secrets"]) != ({reference} if reference else set()):
            raise RuntimePolicyError("gateway_credential_scope_mismatch", "Gateway secret grants must match its approved credential reference exactly.")

    def summary(self):
        document = self.to_document()
        return {key: document[key] for key in ("contract", "provider", "endpoint", "addresses", "models")} | {
            "route_digest": self.digest, "credential_reference": document.get("credential", ""),
            "credential_delivery": "core_https_header", "execution_status": "native_bridge_available" if document["contract"] == STREAM_CONTRACT else "transport_only"}


def redact(value, secrets):
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [redact(one, secrets) for one in value]
    if isinstance(value, dict):
        return {redact(key, secrets): redact(one, secrets) for key, one in value.items()}
    return value


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to one numeric address; TLS still verifies the approved URL hostname."""
    def __init__(self, route, timeout):
        self.route = route.to_document()
        endpoint = route.endpoint
        # Construct explicitly: create_default_context also honors SSLKEYLOGFILE.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.route.get("ca_pem"):
            context.load_verify_locations(cadata=self.route["ca_pem"])
        else:
            context.load_default_certs()
        context.set_alpn_protocols(["http/1.1"])
        super().__init__(endpoint["host"], endpoint["port"], timeout=timeout, context=context)
        self._abort = threading.Event()
        self._socket_lock = threading.Lock()
        self._active_socket = None

    def _track(self, sock):
        with self._socket_lock:
            self._active_socket = sock
            if self._abort.is_set():
                sock.close()
                raise OSError("Gateway canceled")
        return sock

    def connect(self):
        address = ipaddress.ip_address(self.route["addresses"][0])
        sock = self._track(socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM))
        sock.settimeout(self.timeout)
        sock.connect((str(address), self.port))
        wrapped = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
        self.sock = self._track(wrapped)
        wrapped.do_handshake()

    def abort(self):
        self._abort.set()
        with self._socket_lock:
            sock = self._active_socket
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()


def invoke(route, policy, model, body, secret_values, cancel, deadline=None):
    """One bounded request; no retries. The caller owns durable operation receipts."""
    route.authorize(policy, model)
    config = route.to_document()
    if not isinstance(body, bytes) or len(body) > LIMIT:
        raise RuntimePolicyError("gateway_request_too_large", "The review request exceeds its protocol bound.")
    request = parse_document(body)
    if not isinstance(request, dict) or request.get("model") != model:
        raise RuntimePolicyError("gateway_model_changed", "The request body must use the approved model exactly.")
    if "stream" in request and request["stream"] is not False:
        raise RuntimePolicyError("gateway_streaming_unavailable", "CLI streaming requires the native bridge; this transport increment supports bounded JSON only.")
    if not isinstance(secret_values, SecretValues):
        raise RuntimePolicyError("invalid_gateway_credentials", "Gateway secrets must be explicit scoped values.")
    reference = config.get("credential")
    if set(secret_values.names) != ({reference} if reference else set()):
        raise RuntimePolicyError("gateway_credential_unavailable", "The gateway credential is missing or outside its approved scope.")
    credentials = dict(secret_values.items())
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Connection": "close"}
    if credentials:
        key = credentials[reference]
        if not isinstance(key, str) or not key or len(key) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise RuntimePolicyError("invalid_gateway_credential", "The gateway credential must be a bounded HTTP bearer token.")
        headers["Authorization"] = "Bearer " + key
    cancel.raise_if_set()
    now = time.monotonic()
    if deadline is not None:
        try:
            valid_deadline = type(deadline) in (int, float) and math.isfinite(deadline)
        except OverflowError:
            valid_deadline = False
        if not valid_deadline:
            raise RuntimePolicyError("invalid_gateway_deadline", "The gateway deadline must be a finite monotonic timestamp.")
    until = min(deadline if deadline is not None else math.inf, now + config.get("timeout_seconds", 30))
    if until <= now:
        raise RuntimePolicyError("gateway_timeout", "The review deadline expired before the gateway call.")
    try:
        connection = _PinnedHTTPS(route, until - now)
    except (OSError, ValueError):
        raise RuntimePolicyError("gateway_tls_configuration", "The reviewed gateway CA configuration is invalid.") from None
    done, answer = threading.Event(), []
    def work():
        try:
            connection.request("POST", urlsplit(config["endpoint"]).path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimePolicyError("gateway_http_status", "The gateway did not return HTTP 200; redirects and automatic retries are disabled.")
            if response.getheader("Content-Encoding", "identity").lower() != "identity" or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise RuntimePolicyError("gateway_response_type", "Expected an uncompressed JSON review response.")
            raw = response.read(config.get("response_bytes", LIMIT) + 1)
            if len(raw) > config.get("response_bytes", LIMIT):
                raise RuntimePolicyError("gateway_response_too_large", "The gateway response exceeded its approved bound.")
            value = parse_document(raw)
            answer.append((redact(value, tuple(credentials.values())), None))
        except RuntimePolicyError as exc:
            answer.append((None, exc))
        except (OSError, ValueError, RecursionError, http.client.HTTPException):
            answer.append((None, RuntimePolicyError("gateway_transport_failed", "The gateway transport failed; the request outcome may be uncertain.")))
        finally:
            try:
                connection.abort()
                connection.close()
            finally:
                done.set()
    thread = threading.Thread(target=work, name="qavector-provider-gateway", daemon=True)
    thread.start()
    try:
        while not done.wait(0.02):
            cancel.raise_if_set()
            if time.monotonic() >= until:
                raise RuntimePolicyError("gateway_timeout", "The review gateway deadline expired; automatic retry is blocked.")
        cancel.raise_if_set()
        if time.monotonic() >= until:
            raise RuntimePolicyError("gateway_timeout", "The review gateway deadline expired; automatic retry is blocked.")
        value, error = answer[0]
        if error:
            raise error
        return value
    finally:
        connection.abort()
        thread.join(2)
        if thread.is_alive():
            raise RuntimePolicyError("gateway_cleanup_unconfirmed", "Gateway transport shutdown is unconfirmed; replacement execution is blocked.")
