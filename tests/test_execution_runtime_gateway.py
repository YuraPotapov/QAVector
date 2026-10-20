"""Authorization and real TLS transport; not a claim of native Claude CLI support."""

from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import subprocess
import threading
import time

import pytest

from cycle.context import CancelToken, Cancelled
from execution_runtime import cli
from execution_runtime._json import canonical
from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.gateway import CONTRACT, LIMIT, ProviderGateway, invoke
from execution_runtime.policy import PolicyLayer, PolicyPatch, PolicyResolver
from execution_runtime.secrets import SecretValues


def profile(**changes):
    return dict(contract=CONTRACT, provider="claude_cli", endpoint="https://api.anthropic.com/v1/messages",
        addresses=["1.1.1.1"], models=["sonnet"], credential="CLAUDE_TOKEN", **changes)


def effective(route, **changes):
    raw = {"network": {"mode": "restricted", "endpoints": [route.endpoint]},
           "secrets": [route.to_document()["credential"]] if route.to_document().get("credential") else []}
    raw.update(changes)
    return PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch(raw))], platform="linux", provider="linux-isolated")


@pytest.mark.parametrize("patch", [
    {"provider": "unreviewed"}, {"contract": "unknown"}, {"endpoint": "http://api.example/review"},
    {"endpoint": "https://user:key@api.example/review"}, {"endpoint": "https://api.example/review?token=secret"},
    {"endpoint": "https://api.example/review#fragment"}, {"endpoint": "https://api.example/\nreview"},
    {"endpoint": "https://api.example/\\review"}, {"endpoint": "https://api.example:/review"},
    {"endpoint": "https://api.example:0/review"}, {"endpoint": "https://api.example/review?"},
    {"endpoint": "https://api.example/review#"}, {"endpoint": "https://api.example//review"},
    {"addresses": []}, {"addresses": ["127.0.0.1"]}, {"addresses": ["169.254.169.254"], "allow_private_addresses": True},
    {"addresses": ["::ffff:127.0.0.1"], "allow_private_addresses": True}, {"addresses": ["0.0.0.0"]},
    {"addresses": ["224.0.0.1"]}, {"addresses": ["fe80::1%eth0"]}, {"addresses": ["1.1.1.1", "1.1.1.1"]},
    {"addresses": ["api.anthropic.com"]}, {"models": []}, {"models": ["sonnet", "sonnet"]},
    {"models": [{"model": "sonnet"}]}, {"models": ["sonnet\n"]}, {"timeout_seconds": True},
    {"timeout_seconds": float("nan")}, {"timeout_seconds": 121}, {"timeout_seconds": 10 ** 400},
    {"response_bytes": LIMIT + 1}, {"credential": "bad secret"}, {"extra": "secret"}, {"ca_pem": "invalid"},
])
def test_invalid_routes_fail_closed_without_io(patch):
    raw = profile()
    raw.update(patch)
    with pytest.raises(RuntimePolicyError):
        ProviderGateway(raw)


def test_gateway_profiles_pin_with_deployment_and_do_not_enable_workers(tmp_path):
    raw = {"api_version": "qavector.io/v1", "kind": "RuntimeDeployment", "metadata": {"name": "demo"},
        "spec": {"provider": "linux-isolated", "policy": {}, "bindings": [], "working_directory": "/work",
                 "backend_options": {}, "provider_gateways": {"claude": profile()}}}
    registry = DeploymentRegistry(tmp_path / "deployments")
    first = registry.save(RuntimeDeployment(raw))
    raw["spec"]["provider_gateways"]["claude"]["models"] = ["opus"]
    second = registry.save(RuntimeDeployment(raw), first.digest)
    assert second.digest != first.digest and "workers" not in second.to_document()["spec"]
    with pytest.raises(RuntimePolicyError, match="changed"):
        registry.load("demo", first.digest)
    raw["spec"]["provider_gateways"]["claude"]["addresses"] = ["127.0.0.1"]
    with pytest.raises(RuntimePolicyError):
        RuntimeDeployment(raw)


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_gateway_authorization_contract_has_no_native_path_assumptions(platform):
    route = ProviderGateway(profile())
    raw = {"network": {"mode": "restricted", "endpoints": [route.endpoint]}, "secrets": ["CLAUDE_TOKEN"]}
    policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch(raw))], platform=platform, provider=platform + "-isolated")
    route.authorize(policy, "sonnet")
    assert route.summary()["execution_status"] == "transport_only"


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True, 10 ** 400, 0])
def test_invalid_or_elapsed_deadlines_fail_before_opening_a_socket(monkeypatch, deadline):
    route = ProviderGateway(profile())
    monkeypatch.setattr("execution_runtime.gateway._PinnedHTTPS", lambda *_: pytest.fail("No socket with an invalid deadline"))
    with pytest.raises(RuntimePolicyError) as error:
        invoke(route, effective(route), "sonnet", b'{"model":"sonnet"}', SecretValues({"CLAUDE_TOKEN": "token"}), CancelToken(), deadline)
    assert error.value.code in ("gateway_timeout", "invalid_gateway_deadline")


def test_cancel_before_call_has_no_network_side_effect(monkeypatch):
    token = CancelToken()
    token.set("stopped")
    route = ProviderGateway(profile())
    monkeypatch.setattr("execution_runtime.gateway._PinnedHTTPS", lambda *_: pytest.fail("No socket after Stop"))
    with pytest.raises(Cancelled):
        invoke(route, effective(route), "sonnet", b'{"model":"sonnet"}', SecretValues({"CLAUDE_TOKEN": "token"}), token)


@pytest.mark.parametrize("changes,model,secrets,body,code", [
    ({"network": {"mode": "none"}}, "sonnet", {"CLAUDE_TOKEN": "value"}, {"model": "sonnet"}, "gateway_network_denied"),
    ({"network": {"mode": "full"}}, "sonnet", {"CLAUDE_TOKEN": "value"}, {"model": "sonnet"}, "gateway_network_denied"),
    ({"network": {"mode": "restricted", "endpoints": []}}, "sonnet", {"CLAUDE_TOKEN": "value"}, {"model": "sonnet"}, "gateway_network_denied"),
    ({}, "unapproved", {"CLAUDE_TOKEN": "value"}, {"model": "unapproved"}, "gateway_model_denied"),
    ({"secrets": []}, "sonnet", {"CLAUDE_TOKEN": "value"}, {"model": "sonnet"}, "gateway_credential_scope_mismatch"),
    ({}, "sonnet", {}, {"model": "sonnet"}, "gateway_credential_unavailable"),
    ({}, "sonnet", {"CLAUDE_TOKEN": "value", "OTHER": "other"}, {"model": "sonnet"}, "gateway_credential_unavailable"),
    ({}, "sonnet", {"CLAUDE_TOKEN": "value"}, {"model": "opus"}, "gateway_model_changed"),
    ({}, "sonnet", {"CLAUDE_TOKEN": "value"}, {"model": "sonnet", "stream": True}, "gateway_streaming_unavailable"),
    ({}, "sonnet", {"CLAUDE_TOKEN": "value\r\nInjected: key"}, {"model": "sonnet"}, "invalid_gateway_credential"),
])
def test_authorization_rejects_before_opening_any_socket(monkeypatch, changes, model, secrets, body, code):
    route = ProviderGateway(profile())
    monkeypatch.setattr("execution_runtime.gateway._PinnedHTTPS", lambda *_: pytest.fail("No socket before authorization"))
    with pytest.raises(RuntimePolicyError) as caught:
        invoke(route, effective(route, **changes), model, canonical(body).encode(), SecretValues(secrets), CancelToken())
    assert caught.value.code == code


@pytest.fixture
def tls_gateway(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.utcnow() - timedelta(days=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
    certificate = tmp_path / "certificate.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key = tmp_path / "key.pem"
    private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    state = {"calls": [], "status": 200, "body": b'{"result":"ok"}', "delay": 0,
             "content_type": "application/json", "encoding": "identity", "body_delay": 0}
    received = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"].append((self.path, dict(self.headers), data))
            received.set()
            time.sleep(state["delay"])
            try:
                self.send_response(state["status"])
                self.send_header("Content-Type", state["content_type"])
                self.send_header("Content-Encoding", state["encoding"])
                self.send_header("Location", "https://unapproved.invalid/")
                self.send_header("Content-Length", str(len(state["body"])))
                self.end_headers()
                self.wfile.flush()
                time.sleep(state["body_delay"])
                self.wfile.write(state["body"])
            except OSError:
                pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(private_key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    raw = profile()
    raw.update(endpoint="https://localhost:%s/v1/messages" % server.server_port, addresses=["127.0.0.1"],
               allow_private_addresses=True, ca_pem=certificate.read_text(), timeout_seconds=3)
    try:
        yield raw, state, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def call(raw, cancel=None, deadline=None):
    route = ProviderGateway(raw)
    return invoke(route, effective(route), "sonnet", b'{"model":"sonnet","messages":[]}',
                  SecretValues({"CLAUDE_TOKEN": "synthetic-token"}), cancel or CancelToken(), deadline)


def test_real_tls_uses_ip_pin_verified_hostname_and_only_scoped_headers(tls_gateway, tmp_path, monkeypatch):
    raw, state, _ = tls_gateway
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("No DNS resolution during egress"))
    monkeypatch.setenv("HTTPS_PROXY", "http://unapproved.invalid:12345")
    key_log = tmp_path / "must-not-write-keys"
    monkeypatch.setenv("SSLKEYLOGFILE", str(key_log))
    assert call(raw) == {"result": "ok"}
    assert len(state["calls"]) == 1
    path, headers, body = state["calls"][0]
    assert path == "/v1/messages" and headers["Host"].startswith("localhost:")
    assert headers["Authorization"] == "Bearer synthetic-token"
    assert "synthetic-token" not in body.decode() and not key_log.exists()
    assert not {"Cookie", "Proxy-Authorization"} & set(headers)


@pytest.mark.parametrize("change,code", [
    ({"status": 302}, "gateway_http_status"), ({"status": 429}, "gateway_http_status"),
    ({"body": b"x" * (LIMIT + 1)}, "gateway_response_too_large"),
    ({"body": b'{"duplicate":1,"duplicate":2}'}, "duplicate_key"),
    ({"body": b'{"value":NaN}'}, "invalid_number"),
    ({"encoding": "gzip"}, "gateway_response_type"), ({"content_type": "text/event-stream"}, "gateway_response_type"),
])
def test_remote_errors_never_follow_redirects_or_retry(tls_gateway, change, code):
    raw, state, _ = tls_gateway
    state.update(change)
    with pytest.raises(RuntimePolicyError) as caught:
        call(raw)
    assert caught.value.code == code and len(state["calls"]) == 1


def test_bad_certificate_or_hostname_never_sends_credentials(tls_gateway):
    raw, state, _ = tls_gateway
    raw["endpoint"] = raw["endpoint"].replace("localhost", "unapproved.invalid")
    with pytest.raises(RuntimePolicyError) as caught:
        call(raw)
    assert caught.value.code == "gateway_transport_failed" and state["calls"] == []
    raw["endpoint"] = raw["endpoint"].replace("unapproved.invalid", "localhost")
    raw.pop("ca_pem")
    with pytest.raises(RuntimePolicyError):
        call(raw)
    assert state["calls"] == []


def test_failed_first_ip_pin_does_not_retry_another_address(tls_gateway):
    raw, state, _ = tls_gateway
    raw["addresses"] = ["127.0.0.2", "127.0.0.1"]
    with pytest.raises(RuntimePolicyError):
        call(raw)
    assert state["calls"] == []


def test_response_secret_echo_is_redacted_after_json_decoding(tls_gateway):
    raw, state, _ = tls_gateway
    state["body"] = b'{"echo":"synthetic-\\u0074oken","nested":["synthetic-token"]}'
    assert call(raw) == {"echo": "[redacted]", "nested": ["[redacted]"]}


def test_deadline_also_interrupts_a_blocked_response_body(tls_gateway):
    raw, state, _ = tls_gateway
    state["body_delay"] = 1
    started = time.monotonic()
    with pytest.raises(RuntimePolicyError):
        call(raw, deadline=time.monotonic() + 0.2)
    assert time.monotonic() - started < 0.8
    assert len(state["calls"]) == 1
    assert not any(t.name == "qavector-provider-gateway" for t in threading.enumerate())


@pytest.mark.parametrize("stop", [False, True], ids=["deadline", "cancel"])
def test_cancel_and_deadline_abort_a_pending_call(tls_gateway, stop):
    raw, state, received = tls_gateway
    state["delay"] = 1
    token = CancelToken()
    def cancel():
        assert received.wait(2)
        token.set("operator stopped")
    thread = threading.Thread(target=cancel) if stop else None
    if thread:
        thread.start()
    started = time.monotonic()
    with pytest.raises(Cancelled if stop else RuntimePolicyError) as caught:
        call(raw, token, deadline=time.monotonic() + (3 if stop else 0.2))
    if not stop:
        assert caught.value.code in ("gateway_timeout", "gateway_transport_failed")
    assert time.monotonic() - started < 0.8 and len(state["calls"]) == 1
    if thread:
        thread.join(2)
    assert not any(t.name == "qavector-provider-gateway" for t in threading.enumerate())


def test_cli_validation_is_offline_and_reports_transport_only(tmp_path, capsys):
    source = tmp_path / "gateway.json"
    source.write_text(canonical(profile()))
    assert cli.main(["--runtime-gateway-validate", "--from=" + str(source)]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["gateway"]["provider"] == "claude_cli"
    assert value["gateway"]["execution_status"] == "transport_only"
    assert value["enforcement"] == "not_evaluated"


def test_frozen_core_includes_gateway_validation_and_strict_ca_check(tmp_path, request, monkeypatch):
    binary = request.config.getoption("--runtime-frozen-core")
    if not binary:
        pytest.skip("Pass --runtime-frozen-core for packaged gateway validation")
    monkeypatch.setenv("CMS_HOME", str(tmp_path / "control"))
    source = tmp_path / "gateway.json"
    source.write_text(canonical(profile()))
    result = subprocess.run([binary, "--runtime-gateway-validate", "--from=" + str(source)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["gateway"]["execution_status"] == "transport_only"
    raw = profile()
    raw["ca_pem"] = "invalid"
    source.write_text(canonical(raw))
    result = subprocess.run([binary, "--runtime-gateway-validate", "--from=" + str(source)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert json.loads(result.stdout)["problems"][0]["code"] == "invalid_gateway_ca"
