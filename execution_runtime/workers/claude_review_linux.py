"""Pinned isolated CLI bridge. Only framed data crosses the private core pipes."""

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

CONTRACT = "qavector.agent-review/v1"
LIMIT = 65536
wire_lock = threading.Lock()
failed = threading.Event()


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


def main():
    server = process = None
    review, code = None, "claude_worker_failed"
    try:
        if sys.platform != "linux" or len(sys.argv) != 2 or len(sys.argv[1].encode()) > LIMIT:
            raise ValueError("invocation")
        config = decode(sys.argv[1])
        if not isinstance(config, dict) or set(config) != {"contract", "claude", "cwd", "model", "prompt", "files", "capability", "max_requests", "request_bytes"} or config["contract"] != CONTRACT:
            raise ValueError("request")
        if (not isinstance(config["files"], list) or len(config["files"]) > 16 or
                not all(isinstance(value, str) and Path(value).is_absolute() for value in [config["claude"], config["cwd"]] + config["files"])):
            raise ValueError("paths")
        prompt = config["prompt"].replace("{{QAVECTOR_NATIVE_EVIDENCE}}", evidence(config["files"]))
        if len(prompt.encode()) > 131072:
            raise ValueError("prompt_bound")
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
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory(prefix="claude-home-") as home, tempfile.TemporaryFile() as output:
            env = {"HOME": home, "CLAUDE_CONFIG_DIR": home, "TMPDIR": "/tmp", "PATH": "/nonexistent",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % server.server_port,
                "CLAUDE_CODE_OAUTH_TOKEN": config["capability"], "CLAUDE_CODE_GZIP_REQUEST_BODIES": "0",
                "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_SAFE_MODE": "1"}
            args = [config["claude"], "-p", "--output-format", "stream-json", "--verbose", "--model", config["model"],
                "--restricted", "--safe-mode", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", '{"disableAllHooks":true}', "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
                "--no-session-persistence"]
            process = subprocess.Popen(args, cwd=config["cwd"], env=env, stdin=subprocess.PIPE, stdout=output, stderr=sys.stderr)
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
            if process.returncode or failed.is_set():
                raise ValueError("cli_failed")
            output.seek(0)
            raw = output.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("cli_output_bound")
            result = None
            for line in raw.splitlines():
                event = decode(line)
                if isinstance(event, dict) and event.get("type") == "result":
                    result = event
            if not result or result.get("is_error"):
                raise ValueError("cli_result")
            review = result.get("structured_output")
            if review is None:
                text = result.get("result", "").strip()
                start = text.find("{")
                if start < 0:
                    raise ValueError("review_json")
                review, _ = json.JSONDecoder().raw_decode(text[start:])
            if not isinstance(review, dict):
                raise ValueError("review_json")
            code = ""
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt):
        review = None
    finally:
        if process is not None and process.poll() is None:
            process.kill(); process.wait()
        if server is not None:
            server.shutdown(); server.server_close()
    raw = json.dumps({"contract": CONTRACT, "review": review, "code": code}, ensure_ascii=True, allow_nan=False).encode()
    if len(raw) > LIMIT:
        raw = json.dumps({"contract": CONTRACT, "review": None, "code": "review_packet_too_large"}).encode()
    packet({"kind": "output", "body": base64.b64encode(raw).decode()})
    return 0 if review is not None and not code else 1


if __name__ == "__main__":
    def interrupted(_signal, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
