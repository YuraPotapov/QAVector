"""Standalone protocol semantics; native boundary proofs are in Linux acceptance."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from execution_runtime.workers import AGENT_CONTRACT, agent_artifact

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux review worker artifact")


@pytest.fixture
def review_request(tmp_path):
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("review evidence")
    provider = Path(__file__).parent / "fixtures/runtime/review_provider.py"
    return dict(contract=AGENT_CONTRACT, cwd=str(tmp_path), provider=[sys.executable, "-I", "-B", str(provider)],
        request=dict(contract="qavector.review-provider/v1", task="Review fixture.", model="local-fixture", repository=str(tmp_path),
            inputs={}, files=[str(evidence)], assess="complexity", scope=True, verdict=True, clarifications=[]))


def execute(value, env=None):
    path, _ = agent_artifact("linux")
    done = subprocess.run([sys.executable, "-I", "-B", str(path), json.dumps(value)], capture_output=True, text=True, timeout=10, env=env)
    return done, json.loads(done.stdout)


def test_review_passes_bounded_evidence_and_does_not_inherit_provider_environment(review_request):
    done, result = execute(review_request, dict(os.environ, HOST_AGENT_SECRET="must-not-forward"))
    assert done.returncode == 0, (result, done.stderr)
    assert result["review"]["ready"] is True and result["review"]["complexity"] == "low"
    assert not result["code"]


def test_review_v2_passes_only_credential_file_metadata(review_request, tmp_path):
    credential = tmp_path / "credential"
    credential.write_text("standalone-synthetic-token")
    review_request["request"].update(contract="qavector.review-provider/v2", credential_files={"api_key": str(credential)})
    done, result = execute(review_request)
    assert done.returncode == 0, (result, done.stderr)
    assert "standalone-synthetic-token" not in done.stdout + done.stderr


@pytest.mark.parametrize("files", [{"api_key": "relative-path"}, {"bad alias": "/credential"}, ["/credential"]])
def test_review_v2_rejects_malformed_credential_file_metadata(review_request, files):
    review_request["request"].update(contract="qavector.review-provider/v2", credential_files=files)
    done, result = execute(review_request)
    assert done.returncode != 0 and result["code"] == "invalid_credential_files"


def test_review_refuses_oversized_evidence_without_starting_provider(review_request):
    Path(review_request["request"]["files"][0]).write_text("x" * 8193)
    done, result = execute(review_request)
    assert done.returncode == 1 and result["code"] == "review_evidence_too_large"


def test_review_refuses_fifo_instead_of_blocking(review_request, tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    review_request["request"]["files"] = [str(fifo)]
    done, result = execute(review_request)
    assert done.returncode == 1 and result["code"] == "evidence_not_regular"


@pytest.mark.parametrize("reply,code", [
    ('{"risk":"low","risk":"high"}', "duplicate_json_key"),
    ('{"risk":NaN}', "invalid_json_number"),
    ('x' * 65537, "review_packet_too_large"),
], ids=["duplicate", "nonfinite", "oversize"])
def test_review_provider_protocol_is_bounded_and_duplicate_free(review_request, tmp_path, reply, code):
    provider = tmp_path / "provider.py"
    provider.write_text("import sys\nsys.stdin.read()\nsys.stdout.write(" + repr(reply) + ")\n")
    review_request["provider"] = [sys.executable, "-I", "-B", str(provider)]
    done, result = execute(review_request)
    assert done.returncode == 1 and result["code"] == code
