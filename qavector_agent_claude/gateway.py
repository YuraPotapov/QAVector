"""Read-only Claude transport inside a pinned provider-gateway worker.

Only the host gateway holds the real credential. This transport sees a scoped
capability and private loopback; the Runtime OS boundary governs descendants.
It does not use the generic worker's stdio RPC channel.
"""

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit


from qavector_sdk import ContractError, preflight, validate_contract
from qavector_sdk.ipc import failure
from .backend import BACKEND_ID, VERSION, ClaudeCliBackend, config_schema
from .command import command, gateway_command

CONTRACT = "qavector.agent-review/v1"
LIMIT = 65536
wire_lock = threading.Lock()

def packet(value):
    with wire_lock:
        sys.stdout.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
        sys.stdout.flush()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("nonfinite JSON")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def evidence(paths):
    parts, total = [], 0
    for path in paths:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("evidence_not_regular")
            raw = stream.read(8193)
        total += len(raw)
        if len(raw) > 8192 or total > 32768:
            raise ValueError("evidence_too_large")
        parts.extend(("# File: " + path, "```", raw.decode("utf-8", "replace"), "```"))
    return "\n".join(parts)


class GatewayTransport:
    streaming = False  # The bounded CLI log is normalized after local exit.

    def __init__(self, config):
        self.config = config
        self.model = config["profile"]["model"]
        self.failed = threading.Event()
        self.process = self.server = None

    async def prerequisites(self, profile, context):
        expected = {"binary": self.config["claude"], "tools": ["Read", "Grep", "Glob"]}
        if (context != self.config["context"] or profile != self.config["profile"]
                or profile["backend_options"] != expected or not profile["model"]
                or profile["limits"] or profile["credentials_ref"] is not None):
            raise ContractError("POLICY_UNSATISFIED", "Profile differs from the approved gateway scope")
        if not os.path.isfile(expected["binary"]) or not os.access(expected["binary"], os.X_OK):
            raise ContractError("AGENT_PREREQUISITE_MISSING", "Approved Claude executable is unavailable")

    async def workspace(self, reference, context):
        if reference != "gateway.workspace" or context != self.config["context"]:
            raise ContractError("POLICY_UNSATISFIED", "Workspace is outside the gateway scope")
        return self.config["cwd"]

    async def run(self, argv, cwd, on_line):
        config = self.config
        prompt = argv[2]
        if (argv != command(config["claude"], prompt, ["Read", "Grep", "Glob"], model=self.model)
                or cwd != config["cwd"] or len(prompt.encode()) > 131072):
            raise ContractError("POLICY_UNSATISFIED", "Command differs from the approved read-only gateway scope")
        try:
            return await self._run(prompt, on_line)
        finally:
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()

    async def _run(self, prompt, on_line):
        config, failed = self.config, self.failed
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            count = 0
            def do_POST(self):
                try:
                    self.connection.settimeout(120)
                    target = urlsplit(self.path)
                    if (target.path != "/v1/messages" or target.query not in ("", "beta=true") or
                            self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding", "identity") != "identity" or
                            len(self.headers.get_all("Content-Length", [])) != 1):
                        raise ValueError("path/encoding")
                    size = int(self.headers["Content-Length"])
                    if not 1 <= size <= config["request_bytes"] or Handler.count >= config["max_requests"] or failed.is_set():
                        raise ValueError("request_bound")
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise ValueError("request_truncated")
                    Handler.count += 1
                    headers = {name: self.headers[name] for name in ("authorization", "anthropic-version", "anthropic-beta", "x-app", "user-agent") if self.headers.get(name) is not None}
                    packet({"kind": "request", "id": Handler.count, "path": target.path, "headers": headers,
                            "body": base64.b64encode(body).decode()})
                    started, total = False, 0
                    while True:
                        raw = sys.stdin.buffer.readline(65537)
                        if not raw or len(raw) > 65536:
                            raise ValueError("core_channel_closed")
                        response = decode(raw)
                        if response.get("id") != Handler.count:
                            raise ValueError("core_channel_id")
                        if response["kind"] == "headers" and not started:
                            if response.get("content_type") not in ("application/json", "text/event-stream"):
                                raise ValueError("response_type")
                            self.send_response(200)
                            self.send_header("Content-Type", response["content_type"])
                            self.send_header("Connection", "close")
                            self.end_headers()
                            started = True
                        elif response["kind"] == "data" and started:
                            chunk = base64.b64decode(response["body"], validate=True)
                            total += len(chunk)
                            if total > 1048576:
                                raise ValueError("response_bound")
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        elif response["kind"] == "end" and started:
                            break
                        else:
                            raise ValueError("provider_channel_error")
                except (ValueError, OSError, KeyError, TypeError):
                    failed.set()
                    self.close_connection = True
                    try:
                        self.send_error(403, "Provider capability unavailable")
                    except OSError:
                        pass
            def log_message(self, *_):
                pass
        server = self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory(prefix="claude-home-") as home, tempfile.TemporaryFile() as output:
            env = {"HOME": home, "CLAUDE_CONFIG_DIR": home, "TMPDIR": "/tmp", "PATH": "/nonexistent",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % server.server_port,
                "CLAUDE_CODE_OAUTH_TOKEN": config["capability"], "CLAUDE_CODE_GZIP_REQUEST_BODIES": "0",
                "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_SAFE_MODE": "1"}
            args = gateway_command(config["claude"], self.model)
            process = self.process = subprocess.Popen(args, cwd=config["cwd"], env=env, stdin=subprocess.PIPE, stdout=output, stderr=sys.stderr)
            process.stdin.write(prompt.encode())
            process.stdin.close()
            while process.poll() is None:
                if failed.wait(0.05):
                    process.terminate()
                    break
                if output.tell() > 1048576:
                    process.terminate()
                    failed.set()
                    break
            process.wait(timeout=3)
            output.seek(0)
            raw = output.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("cli_output_bound")
            for line in raw.splitlines():
                decode(line)  # Preserve strict duplicate-key/nonfinite rejection.
                await on_line(line)
            return {"ok": not process.returncode and not failed.is_set(), "started": True,
                    "message": "Claude gateway execution failed"}


def validate_config(config):
    fields = {"contract", "claude", "cwd", "files", "capability", "max_requests", "request_bytes",
              "profile", "request", "context"}
    if not isinstance(config, dict) or set(config) != fields or config["contract"] != CONTRACT:
        raise ValueError("request")
    if (not isinstance(config["files"], list) or len(config["files"]) > 16 or
            not all(isinstance(value, str) and Path(value).is_absolute()
                    for value in [config["claude"], config["cwd"]] + config["files"])):
        raise ValueError("paths")
    if (type(config["max_requests"]) is not int or not 1 <= config["max_requests"] <= 16 or
            type(config["request_bytes"]) is not int or not 1 <= config["request_bytes"] <= 524288 or
            not isinstance(config["capability"], str) or not config["capability"].startswith("sk-ant-oat01-runtime-")
            or len(config["capability"]) > 128):
        raise ValueError("gateway_bounds")
    for name, key in (("agent-profile", "profile"), ("agent-request", "request"), ("execution-context", "context")):
        validate_contract(name, config[key])


async def execute(config):
    validate_config(config)
    request = dict(config["request"])
    request["task"] = request["task"].replace("{{QAVECTOR_NATIVE_EVIDENCE}}", evidence(config["files"]))
    transport = GatewayTransport(config)
    backend = ClaudeCliBackend(transport)

    async def receive(event):
        # Egress owns stdio; do not mix SDK notifications into its framing.
        validate_contract("agent-event", event)

    try:
        capabilities = await backend.describe(config["profile"], config["context"])
        preflight(config["profile"], request, capabilities,
                  {"schema_version": "1.0.0", "workspace_access": "read"}, config_schema=config_schema())
        result = await backend.execute(request, config["context"], receive)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result = {"schema_version": "1.0.0", "attempt_id": request["attempt_id"],
                  "backend_ref": BACKEND_ID, "plugin_version": VERSION,
                  "status": "unknown" if transport.process is not None else "failed",
                  "summary": "Claude gateway execution could not produce a validated result",
                  "output": None, "artifact_refs": [], "session_ref": None, "usage": None,
                  "error": failure("EXECUTION_STATE_UNKNOWN" if transport.process is not None else
                                   exc.code if isinstance(exc, ContractError) else "AGENT_EXECUTION_FAILED",
                                   "Claude gateway execution could not produce a validated result")}
    validate_contract("agent-result", result)
    return {"contract": CONTRACT, "agent_result": result,
            "provider_result_received": backend.last_answer is not None, "code": ""}


def main():
    output = {"contract": CONTRACT, "agent_result": None, "provider_result_received": False,
              "code": "claude_worker_failed"}
    try:
        if sys.platform != "linux" or len(sys.argv) != 2 or len(sys.argv[1].encode()) > LIMIT:
            raise ValueError("invocation")
        from qavector_sdk.event_loop import run
        output = run(execute(decode(sys.argv[1])))
    except (ValueError, OSError, KeyError, TypeError, KeyboardInterrupt):
        pass
    raw = json.dumps(output, ensure_ascii=True, allow_nan=False).encode()
    oversized = len(raw) > LIMIT
    if oversized:
        raw = json.dumps({"contract": CONTRACT, "agent_result": None,
                          "provider_result_received": False, "code": "review_packet_too_large"}).encode()
    packet({"kind": "output", "body": base64.b64encode(raw).decode()})
    result = output["agent_result"]
    return 0 if result is not None and result["status"] == "completed" and not oversized else 1
