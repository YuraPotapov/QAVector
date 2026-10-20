"""Native broker/CLI proofs. Fixture service never calls an external provider."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading
import time

import pytest

from execution_runtime._json import canonical
from execution_runtime.gateway import ProviderGateway, STREAM_CONTRACT
from execution_runtime.gateway_session import GatewaySession, StreamRedactor
from execution_runtime.secrets import SecretValues
from execution_runtime.errors import RuntimePolicyError
from test_execution_runtime_gateway import tls_gateway, effective


def route_profile(raw):
    return dict(raw, contract=STREAM_CONTRACT, authentication="claude-oauth-token", models=["local-fixture"],
                request_bytes=524288, response_bytes=1048576, max_requests=2, timeout_seconds=15)


def test_streaming_redaction_preserves_split_escape_boundaries():
    token = 'key-"value"-tail'
    redactor = StreamRedactor([token])
    raw = ('prefix ' + json.dumps(token)[1:-1] + ' suffix').encode()
    output = b"".join(redactor.feed(raw[i:i+3]) for i in range(0, len(raw), 3)) + redactor.feed(b"", final=True)
    assert output == b"prefix [redacted] suffix"


def test_session_streams_once_and_rejects_replay(tls_gateway):
    raw, state, _ = tls_gateway
    raw = route_profile(raw)
    state["content_type"] = "text/event-stream"
    state["body"] = b'data: {"text":"synthetic-token"}\n\n'
    route = ProviderGateway(raw)
    session = GatewaySession(route, effective(route), "local-fixture", SecretValues({"CLAUDE_TOKEN": "synthetic-token"}))
    session.bind()
    packet = {"kind": "request", "id": 1, "path": "/v1/messages", "headers": {"authorization": "Bearer " + session.capability},
        "body": base64.b64encode(b'{"model":"local-fixture","stream":true}').decode()}
    frames = []
    try:
        session.handle(packet, frames.append)
        assert frames[0]["kind"] == "headers" and frames[-1]["kind"] == "end"
        data = b"".join(base64.b64decode(one["body"]) for one in frames if one["kind"] == "data")
        assert b"synthetic-token" not in data and b"[redacted]" in data
        packet["id"] = 2
        with pytest.raises(RuntimePolicyError, match="identical"):
            session.handle(packet, frames.append)
        assert len(state["calls"]) == 1
    finally:
        session.close()


@pytest.mark.parametrize("changes", [
    {"authentication":"api-key"}, {"credential":""}, {"endpoint":"https://api.example/other"},
    {"max_requests":True}, {"max_requests":17}, {"request_bytes":524289}, {"response_bytes":1048577},
])
def test_streaming_route_rejects_unapproved_options(changes):
    from test_execution_runtime_gateway import profile
    raw = route_profile(profile())
    raw.update(changes)
    with pytest.raises(RuntimePolicyError):
        ProviderGateway(raw)


@pytest.mark.parametrize("changes", [
    {"path":"/v1/messages?alternate=1"}, {"headers":{"authorization":"Bearer other"}},
    {"headers":{"host":"other"}}, {"id":True}, {"body":"not base64"},
    {"body":base64.b64encode(b'{"model":"other"}').decode()},
    {"body":base64.b64encode(b'{"model":"local-fixture","model":"local-fixture"}').decode()},
])
def test_unapproved_frame_never_opens_transport(tls_gateway, monkeypatch, changes):
    raw, state, _ = tls_gateway
    route = ProviderGateway(route_profile(raw))
    session = GatewaySession(route, effective(route), "local-fixture", SecretValues({"CLAUDE_TOKEN":"synthetic-token"}))
    monkeypatch.setattr("execution_runtime.gateway_session._PinnedHTTPS", lambda *_: pytest.fail("No transport for invalid frames"))
    frame = {"kind":"request", "id":1, "path":"/v1/messages", "headers":{"authorization":"Bearer " + session.capability},
        "body":base64.b64encode(b'{"model":"local-fixture"}').decode()}
    frame.update(changes)
    with pytest.raises(RuntimePolicyError):
        session.handle(frame, lambda _: None)
    assert not state["calls"]
    session.release()


@pytest.mark.parametrize("deadline", [float("nan"),float("inf"),True,10 ** 400,0])
def test_invalid_session_deadlines_fail_before_io(tls_gateway, deadline):
    raw, _, _ = tls_gateway
    route = ProviderGateway(route_profile(raw))
    with pytest.raises(RuntimePolicyError):
        GatewaySession(route,effective(route),"local-fixture",SecretValues({"CLAUDE_TOKEN":"token"}),deadline)


def test_windows_gateway_stays_a_stub(tls_gateway):
    from execution_runtime.backends.windows import WindowsBackend
    raw, _, _ = tls_gateway
    route = ProviderGateway(route_profile(raw))
    from execution_runtime.policy import PolicyLayer, PolicyPatch, PolicyResolver
    policy = PolicyResolver().resolve([PolicyLayer("platform",PolicyPatch({
        "network":{"mode":"restricted","endpoints":[route.endpoint]},"secrets":["CLAUDE_TOKEN"]}))],
        platform="windows",provider="windows-isolated")
    with pytest.raises(RuntimePolicyError) as error:
        WindowsBackend().gateway_preflight(policy,route)
    assert error.value.code == "gateway_channel_unavailable"


if sys.platform == "linux":
    from test_execution_runtime_linux import native, git_workload_image


@pytest.fixture
def cli_deployment(native, git_workload_image, tls_gateway, tmp_path, monkeypatch):
    from execution_runtime.backends.linux import RuntimeImage
    from execution_runtime.deployments import DeploymentRegistry, RuntimeDeployment
    from execution_runtime.workers import claude_artifact
    from cycle import runtime as cycle_runtime
    from execution_runtime.registry import EnvironmentRegistry
    raw_route, state, received = tls_gateway
    raw_route = route_profile(raw_route)
    base_image, git_worker = git_workload_image
    interpreter = git_worker["argv"][0]
    fixture = tmp_path / "cli.py"
    fixture.write_text('''import http.client,json,os,socket,sys
from urllib.parse import urlsplit
prompt=sys.stdin.read()
assert 'HOST_AGENT_SECRET' not in os.environ
assert os.environ['CLAUDE_CODE_OAUTH_TOKEN'] != 'synthetic-token'
for family,kind in ((socket.AF_UNIX,socket.SOCK_STREAM),(socket.AF_INET,socket.SOCK_DGRAM),(socket.AF_NETLINK,socket.SOCK_RAW)):
 try: socket.socket(family,kind)
 except OSError: pass
 else: raise AssertionError('forbidden socket')
try: socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)
except OSError: pass
else: raise AssertionError('raw IP socket')
for address in (('1.1.1.1',443),('169.254.169.254',80)):
 sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM);sock.settimeout(.2)
 try: sock.connect(address)
 except OSError: pass
 else: raise AssertionError('external interface')
 finally: sock.close()
assert 'approved evidence' in prompt
u=urlsplit(os.environ['ANTHROPIC_BASE_URL'])
c=http.client.HTTPConnection(u.hostname,u.port,timeout=20)
c.request('POST','/v1/messages',json.dumps({'model':'local-fixture','stream':True,'messages':[{'role':'user','content':prompt}]}),headers={'Authorization':'Bearer '+os.environ['CLAUDE_CODE_OAUTH_TOKEN'],'anthropic-version':'2023-06-01','anthropic-beta':'oauth-2025-04-20'})
r=c.getresponse();assert r.status==200
data=r.read().decode();c.close()
review=json.loads(data)
print(json.dumps({'type':'result','is_error':False,'result':json.dumps(review)}))
''')
    fixture.write_text(fixture.read_text().replace("assert 'approved evidence' in prompt", """sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM);sock.settimeout(.2)
try: sock.connect(('127.0.0.1',%d))
except OSError: pass
else: raise AssertionError('host loopback visible')
finally: sock.close()
assert 'approved evidence' in prompt""" % ProviderGateway(raw_route).endpoint["port"]))
    shim = tmp_path / "claude-shim"
    shim.write_text('#!/bin/sh\nexec ' + interpreter + ' -I -B /fixtures/cli.py "$@"\n')
    artifact, _ = claude_artifact("linux")
    entry = "/qavector/workers/claude_review_linux.py"
    entries = [(one.source, one.target, one.executable) for one in base_image.files]
    entries += [(artifact, entry, False), (shim, "/approved/claude", True), (fixture, "/fixtures/cli.py", False)]
    extensions = Path(sysconfig.get_config_var("DESTSHARED"))
    for name in ("_socket", "_ssl", "array"):
        for path in extensions.glob(name + ".*.so"):
            entries.append((str(path), str(path), False))
            import re
            output = subprocess.run(["ldd", str(path)], capture_output=True, text=True).stdout
            for line in output.splitlines():
                match = re.search(r"(?:=>\s+|^\s*)(/\S+)", line)
                if match and Path(match.group(1)).is_file():
                    entries.append((match.group(1), match.group(1), os.access(match.group(1), os.X_OK)))
    entries = list({target: (source,target,executable) for source,target,executable in entries}.values())
    image = RuntimeImage.from_files(entries)
    project, control = tmp_path / "project", tmp_path / "control"
    project.mkdir(); control.mkdir()
    (project / "code.txt").write_text("approved evidence")
    state["body"] = b'{"summary":"synthetic-token reviewed","risk":"low","issues":[],"recommendations":[]}'
    spec = {"provider": "linux-isolated", "working_directory": "/workspace",
        "policy": {"filesystem": [{"binding": "project", "access": "read_only"}],
            "network": {"mode": "restricted", "endpoints": [ProviderGateway(raw_route).endpoint]},
            "secrets": ["CLAUDE_TOKEN"], "resources": {"scratch_bytes": 33554432}},
        "bindings": [{"name":"project","source":str(project),"target":"/workspace"}],
        "backend_options": {"linux": {"state_directory":str(tmp_path / "state"), "image":{"identity":image.identity,"files":[vars(one) for one in image.files]}}},
        "provider_gateways":{"claude":raw_route},"workers":{"agent.review":{"contract":"qavector.agent-review/v1", "transport":"claude_cli",
            "entrypoint":entry,"argv":[interpreter,"-I","-B",entry],"claude":"/approved/claude","gateway":"claude","credentials":{"oauth_token":"CLAUDE_TOKEN"}}}}
    registry = DeploymentRegistry(control / "deployments")
    deployment = registry.save(RuntimeDeployment({"api_version":"qavector.io/v1","kind":"RuntimeDeployment","metadata":{"name":"claude"},"spec":spec}))
    def factory(one, protected):
        backend = one.backend(protected)
        backend._bundle, backend.launcher = native[0]._bundle, native[0].launcher
        return backend
    original = cycle_runtime.CycleRuntime
    monkeypatch.setattr(cycle_runtime,"CycleRuntime",lambda *a,**kw:original(*a,**kw,deployments=registry,profiles=EnvironmentRegistry(control / "profiles"),backend_factory=factory))
    monkeypatch.setenv("HOST_AGENT_SECRET","must-not-inherit")
    document = {"id":"claude-review","variables":{"CLAUDE_TOKEN":{"secret":True}},"execution":{"runtime":{
        "mode":"isolated","deployment":"claude","revision":deployment.digest,"project_directory":str(project)}},
        "steps":[{"id":"review","plugin":"agent.review","timeout":25,"with":{"framework":"claude_cli","model":"local-fixture","task":"Review evidence.","files":["code.txt"]}}]}
    return document, deployment, state, received, control, registry, interpreter, image


def test_native_cli_bridge_keeps_credentials_on_core_and_records_review(cli_deployment, tmp_path):
    from cycle import executor, model
    document, _, state, _, _, _, _, _ = cli_deployment
    run = executor.run_cycle(model.parse_cycle(document, "claude-review"), str(tmp_path / "run"), secrets={"CLAUDE_TOKEN":"synthetic-token"})
    step = run.steps["review"]
    assert step.status == "success", (step.message, step.runtime)
    assert step.outputs["summary"] == "[redacted] reviewed"
    assert step.runtime["gateway"]["calls"] == 1 and step.runtime["cleanup_confirmed"]
    assert state["calls"][0][1]["Authorization"] == "Bearer synthetic-token"
    for path in (tmp_path / "run").rglob("*"):
        if path.is_file():
            assert b"synthetic-token" not in path.read_bytes(), path


@pytest.mark.parametrize("stop", [False, True], ids=["deadline", "stop"])
def test_native_inflight_call_stops_without_retry(cli_deployment, tmp_path, stop):
    from cycle import executor, model
    from cycle.context import CancelToken
    document, _, state, received, _, _, _, _ = cli_deployment
    state["body_delay"] = 12
    document["steps"][0]["timeout"] = 6
    document["steps"][0].update(retry={"attempts":3}, on_failure="continue")
    cancel = CancelToken()
    def interrupt():
        if received.wait(20):
            cancel.set("user stop")
    thread = threading.Thread(target=interrupt, daemon=True) if stop else None
    if thread:
        thread.start()
    started = time.monotonic()
    run = executor.run_cycle(model.parse_cycle(document, "claude-review"), str(tmp_path / "run"),
        secrets={"CLAUDE_TOKEN":"synthetic-token"}, cancel=cancel)
    if thread:
        thread.join(1)
    step = run.steps["review"]
    assert step.status != "success"
    assert len(state["calls"]) == 1 and time.monotonic() - started < 11
    assert step.runtime["cleanup_confirmed"] and step.runtime["gateway"]["closed"]
    assert not any(one.name == "qavector-cli-egress" for one in threading.enumerate())
    from cycle.checkpoints import CheckpointError
    with pytest.raises(CheckpointError, match="Uncertain outcome"):
        executor.run_cycle(model.parse_cycle(document,"claude-review"), str(tmp_path / "run"),
            secrets={"CLAUDE_TOKEN":"synthetic-token"}, resume=True)
    assert len(state["calls"]) == 1


def test_installed_cli_uses_native_bridge_against_local_tls(cli_deployment, tmp_path, request):
    from cycle import executor, model
    from execution_runtime.backends.linux import RuntimeImage
    from execution_runtime.deployments import RuntimeDeployment
    selected = request.config.getoption("--runtime-claude-cli")
    if not selected:
        pytest.skip("Pass --runtime-claude-cli for installed CLI acceptance")
    binary = Path(selected).resolve(strict=True)
    document, deployment, state, _, _, registry, _, image = cli_deployment
    full_model = "claude-sonnet-4-6"
    from examples.execution_runtime.prepare_claude_image import prepare
    shell = next(one.source for one in image.files if one.target == "/bin/busybox")
    prepared = prepare(tmp_path / "prepared-image",binary,shell)
    image_document = json.loads(Path(prepared["image"]).read_text())
    raw = deployment.to_document()
    raw["spec"]["backend_options"]["linux"]["image"] = image_document
    raw["spec"]["policy"]["resources"]["scratch_bytes"] = 134217728
    raw["spec"]["provider_gateways"]["claude"]["models"] = [full_model]
    pinned = registry.save(RuntimeDeployment(raw), deployment.digest)
    document["execution"]["runtime"]["revision"] = pinned.digest
    document["steps"][0]["with"]["model"] = full_model
    document["steps"][0]["timeout"] = 35
    review = json.dumps({"summary":"Local CLI acceptance","risk":"low","issues":[],"recommendations":[]})
    events = [
        ("message_start", {"type":"message_start","message":{"id":"msg_local_fixture","type":"message","role":"assistant","content":[],"model":full_model,"stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":10,"output_tokens":0}}}),
        ("content_block_start", {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}),
        ("content_block_delta", {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":review}}),
        ("content_block_stop", {"type":"content_block_stop","index":0}),
        ("message_delta", {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":None},"usage":{"output_tokens":20}}),
        ("message_stop", {"type":"message_stop"}),
    ]
    state["content_type"] = "text/event-stream"
    state["body"] = "".join("event: %s\ndata: %s\n\n" % (name,json.dumps(value)) for name,value in events).encode()
    run = executor.run_cycle(model.parse_cycle(document,"claude-review"), str(tmp_path / "run"), secrets={"CLAUDE_TOKEN":"synthetic-token"})
    step = run.steps["review"]
    assert step.status == "success", (step.message,step.runtime)
    assert step.outputs["summary"] == "Local CLI acceptance"
    assert len(state["calls"]) == 1 and step.runtime["cleanup_confirmed"]
    assert json.loads(state["calls"][0][2])["model"] == full_model
    assert state["calls"][0][1]["Authorization"] == "Bearer synthetic-token"


def test_frozen_core_includes_worker_and_streaming_bridge(cli_deployment, tmp_path, request):
    from execution_runtime.deployments import DeploymentRegistry
    core = request.config.getoption("--runtime-frozen-core")
    if not core:
        pytest.skip("Pass --runtime-frozen-core for packaged Claude bridge acceptance")
    document, deployment, state, _, _, _, _, _ = cli_deployment
    home, cycles = tmp_path / "user-data", tmp_path / "cycles"
    DeploymentRegistry(home / "execution-deployments").save(deployment)
    cycles.mkdir()
    (cycles / "claude-review.yaml").write_text(json.dumps(document))
    env = dict(os.environ,CMS_HOME=str(home),PATH="/no-system-bwrap")
    secret = tmp_path / "secret.json"
    descriptor = os.open(secret,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
    with os.fdopen(descriptor,"w") as stream:
        json.dump({"value":"synthetic-token"},stream)
    try:
        saved = subprocess.run([core,"--cycle-secret-set=claude-review:CLAUDE_TOKEN","--from=" + str(secret)],
            env=env,capture_output=True,text=True,timeout=15)
        assert saved.returncode == 0, (saved.stdout,saved.stderr)
    finally:
        secret.unlink()
    done = subprocess.run([core,"--cycle-run=claude-review","--cycles-dir=" + str(cycles),"--events=-"],
        env=env,capture_output=True,text=True,timeout=40)
    events = [json.loads(line) for line in done.stdout.splitlines()]
    assert done.returncode == 0, (events,done.stderr)
    result = next(one for one in events if one["kind"] == "cycle.step.end")
    assert result["outputs"]["summary"] == "[redacted] reviewed"
    assert result["runtime"]["cleanup_confirmed"] and result["runtime"]["gateway"]["calls"] == 1
    assert len(state["calls"]) == 1 and "synthetic-token" not in done.stdout + done.stderr


def test_local_readiness_checks_native_image_without_provider_or_credentials(cli_deployment, native, tmp_path, monkeypatch):
    from execution_runtime import cli
    from execution_runtime.deployments import RuntimeDeployment
    _, deployment, state, _, _, registry, _, _ = cli_deployment
    original = RuntimeDeployment.backend
    def backend(one, protected_paths=()):
        result = original(one,protected_paths)
        result._bundle, result.launcher = native[0]._bundle, native[0].launcher
        return result
    monkeypatch.setattr(RuntimeDeployment,"backend",backend)
    def forbidden(*args, **kwargs):
        pytest.fail("Local readiness must not access credentials, host sign-in or provider traffic")
    monkeypatch.setattr("cycle.secrets.load",forbidden)
    monkeypatch.setattr("cycle.plugins.agent_cli.auth_status",forbidden)
    monkeypatch.setattr("execution_runtime.gateway._PinnedHTTPS",forbidden)
    monkeypatch.setattr("execution_runtime.gateway_session._PinnedHTTPS",forbidden)
    source = tmp_path / "check.json"
    source.write_text(json.dumps({"deployment":deployment.to_document(),"worker":"agent.review","model":"local-fixture"}))
    code, result = cli.command("deployment_check",source=source,deployments_directory=registry.directory)
    assert code == 0 and result["ready"], result
    assert result["credential_access"] == "not_read" and result["provider_request"] == "not_sent"
    assert result["account_acceptance"] == "not_verified" and not state["calls"]


def test_frozen_local_readiness_never_contacts_provider(cli_deployment, tmp_path, request):
    core = request.config.getoption("--runtime-frozen-core")
    if not core:
        pytest.skip("Pass --runtime-frozen-core for packaged local readiness acceptance")
    _, deployment, state, _, _, _, _, _ = cli_deployment
    source = tmp_path / "readiness.json"
    source.write_text(json.dumps({"deployment":deployment.to_document(),"worker":"agent.review","model":"local-fixture"}))
    done = subprocess.run([core,"--runtime-deployment-check","--from=" + str(source)],capture_output=True,text=True,timeout=30,
        env=dict(os.environ,CMS_HOME=str(tmp_path / "fresh-profile"),PATH="/no-system-bwrap"))
    assert done.returncode == 0, (done.stdout,done.stderr)
    result = json.loads(done.stdout)
    assert result["ready"] and result["account_acceptance"] == "not_verified"
    assert result["credential_access"] == "not_read" and result["provider_request"] == "not_sent"
    assert not state["calls"]
