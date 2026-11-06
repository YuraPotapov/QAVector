"""A loopback Jira for tests: canned answers, and a record of every request.

It checks what QAVector sends and what it makes of what comes back, and claims
nothing about Jira itself. ``answers`` maps a path prefix - optionally preceded
by a method, ``"PUT /rest/api/3/issue/QA-1"`` - to a payload, ``(status,
payload)``, or a callable ``(request) -> payload``. Bytes are served as a file.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Request(object):
    def __init__(self, method, path, body, headers):
        self.method, self.path, self.body, self.headers = method, path, body, headers

    @property
    def authorization(self):
        return self.headers.get("Authorization")


class FakeJira(object):
    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.seen = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                kind = self.headers.get("Content-Type") or ""
                body = raw
                if raw and kind.startswith("application/json"):
                    body = json.loads(raw)
                request = Request(method, self.path, body, dict(self.headers))
                fake.seen.append(request)
                payload = fake.answer(request)
                status, value = payload if isinstance(payload, tuple) else (200, payload)
                if callable(value):
                    value = value(request)
                binary = isinstance(value, bytes)
                data = value if binary else (b"" if value is None
                                             else json.dumps(value).encode("utf-8"))
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream" if binary
                                 else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):       # noqa: N802 - the base class's spelling
                self._serve("GET")

            def do_POST(self):      # noqa: N802
                self._serve("POST")

            def do_PUT(self):       # noqa: N802
                self._serve("PUT")

            def do_DELETE(self):    # noqa: N802
                self._serve("DELETE")

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.site = "http://127.0.0.1:%d" % self._server.server_port
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def answer(self, request):
        best, found = -1, None
        for key, payload in self.answers.items():
            method, _, prefix = key.rpartition(" ")
            if method and method != request.method:
                continue
            if request.path.startswith(prefix) and len(prefix) > best:
                best, found = len(prefix), payload
        if found is None:
            return 404, {"errorMessages": ["no such path"]}
        return found

    def paths(self, method=None):
        return [one.path for one in self.seen if method in (None, one.method)]

    def close(self):
        self._server.shutdown()
        self._server.server_close()
