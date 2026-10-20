"""Standalone Linux review adapter. Provider execution and evidence reads stay isolated."""

import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile

CONTRACT = "qavector.agent-review/v1"
PROVIDER = "qavector.review-provider/v1"
LIMIT = 65536


class WorkerError(Exception):
    pass


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise WorkerError("duplicate_json_key")
        result[key] = value
    return result


def invalid_number(_value):
    raise WorkerError("invalid_json_number")


def document(raw):
    if len(raw) > LIMIT:
        raise WorkerError("review_packet_too_large")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_number)


def request(value):
    if (not isinstance(value, dict) or set(value) != {"contract", "provider", "cwd", "request"} or
            value["contract"] != CONTRACT or not isinstance(value["cwd"], str) or not Path(value["cwd"]).is_absolute() or
            not isinstance(value["provider"], list) or not 1 <= len(value["provider"]) <= 32 or
            any(not isinstance(one, str) or not one or "\0" in one for one in value["provider"]) or
            not Path(value["provider"][0]).is_absolute()):
        raise WorkerError("invalid_review_request")
    data = value["request"]
    fields = {"contract", "task", "model", "repository", "inputs", "files", "assess", "scope", "verdict", "clarifications"}
    credentials = data.get("credential_files", {}) if isinstance(data, dict) else {}
    if credentials:
        fields.add("credential_files")
    if (not isinstance(data, dict) or set(data) != fields or
            data["contract"] != ("qavector.review-provider/v2" if credentials else PROVIDER) or
            not isinstance(data["files"], list) or len(data["files"]) > 16):
        raise WorkerError("invalid_review_request")
    if credentials and (not isinstance(credentials, dict) or len(credentials) > 8 or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or
            not isinstance(path, str) or not Path(path).is_absolute() or "\0" in path
            for name, path in credentials.items())):
        raise WorkerError("invalid_credential_files")
    # Files are opened only inside the native boundary. Reject devices/FIFOs and
    # bound every read; the policy, rather than a host path lookup, grants access.
    evidence, total = [], 0
    for name in data["files"]:
        if not isinstance(name, str) or not Path(name).is_absolute() or "\0" in name:
            raise WorkerError("invalid_evidence_path")
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise WorkerError("evidence_not_regular")
            raw = stream.read(8193)
        total += len(raw)
        if len(raw) > 8192 or total > 32768:
            raise WorkerError("review_evidence_too_large")
        evidence.append({"path": name, "text": raw.decode("utf-8", "replace")})
    data = dict(data, files=evidence)
    encoded = json.dumps(data, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(encoded) > LIMIT:
        raise WorkerError("review_packet_too_large")
    return encoded


def run(value, encoded):
    # Provider stdout is protocol data; stderr stays a bounded native log.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(value["provider"], cwd=value["cwd"], env={},
            stdin=subprocess.PIPE, stdout=output, stderr=sys.stderr)
        try:
            process.communicate(encoded)
        except BaseException:
            process.kill()
            process.wait()
            raise
        if process.returncode:
            raise WorkerError("review_provider_failed")
        output.seek(0)
        answer = document(output.read(LIMIT + 1))
    if not isinstance(answer, dict):
        raise WorkerError("invalid_review_result")
    return answer


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    result, code, phase = None, "", "request"
    try:
        if sys.platform != "linux" or len(args) != 1:
            raise WorkerError("unsupported_review_invocation")
        value = document(args[0].encode("utf-8"))
        encoded_request = request(value)
        phase = "provider"
        result = run(value, encoded_request)
    except WorkerError as exc:
        code = str(exc)
    except OSError as exc:
        code = "review_%s_os_error_%d" % (phase, exc.errno or 0)
        print("Review native file unavailable: %r (errno %d)" % (str(exc.filename or "")[:256], exc.errno or 0), file=sys.stderr)
    except (ValueError, UnicodeError, RecursionError):
        code = "review_worker_io_error"
    except KeyboardInterrupt:
        code = "review_worker_interrupted"
    encoded = json.dumps({"contract": CONTRACT, "review": result, "code": code}, ensure_ascii=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > LIMIT:
        result, encoded = None, json.dumps({"contract": CONTRACT, "review": None, "code": "review_packet_too_large"})
    print(encoded)
    return 0 if result is not None and not code else 1


if __name__ == "__main__":
    def interrupted(_signal, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
