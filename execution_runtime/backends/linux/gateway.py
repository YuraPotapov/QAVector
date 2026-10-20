"""Native pipe framing. Raw provider request/response frames never enter log files."""

import base64
import os
import select

from ..._json import canonical, parse_document, object_fields
from ...errors import RuntimePolicyError

FRAME_LIMIT = 1500000


class GatewayOutput:
    def __init__(self, process, session):
        self.process, self.session = process, session
        self.pending = b""
        self.finished = False
        os.set_blocking(process.stdin.fileno(), False)

    def send(self, frame):
        raw = (canonical(frame) + "\n").encode()
        descriptor = self.process.stdin.fileno()
        while raw:
            self.session.raise_if_set()
            if select.select([], [descriptor], [], 0.05)[1]:
                try:
                    raw = raw[os.write(descriptor, raw):]
                except BlockingIOError:
                    pass

    def read1(self, count):
        while not self.pending:
            raw = self.process.stdout.readline(FRAME_LIMIT + 1)
            if not raw:
                return b""
            try:
                if len(raw) > FRAME_LIMIT or self.finished:
                    raise RuntimePolicyError("invalid_gateway_frame", "Native provider framing exceeded its bound.")
                packet = parse_document(raw)
                if isinstance(packet, dict) and packet.get("kind") == "output":
                    object_fields(packet, ("kind", "body"), "gateway.output", ("kind", "body"))
                    self.pending = base64.b64decode(packet["body"], validate=True)
                    if len(self.pending) > 65536:
                        raise ValueError()
                    self.finished = True
                else:
                    try:
                        self.session.handle(packet, self.send)
                    except RuntimePolicyError as error:
                        self.session.failed = True
                        self.send({"kind": "error", "id": packet.get("id", 0) if isinstance(packet, dict) else 0, "code": error.code})
            except (RuntimePolicyError, ValueError, OSError, TypeError):
                self.session.failed = True
                self.session.close()
                raise OSError("Invalid native provider channel") from None
        result, self.pending = self.pending[:count], self.pending[count:]
        return result

    read = read1

    def close(self):
        self.process.stdout.close()
