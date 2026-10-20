"""Explicit deterministic protocol fixture, not an LLM or production provider."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

request = json.load(sys.stdin)
assert request["contract"] in ("qavector.review-provider/v1", "qavector.review-provider/v2")
assert "HOST_AGENT_SECRET" not in os.environ
credential = ""
if request["contract"] == "qavector.review-provider/v2":
    assert set(request["credential_files"]) == {"api_key"}
    source = Path(request["credential_files"]["api_key"])
    credential = source.read_text()
    assert credential
    assert "PROVIDER_TOKEN" not in os.environ
    if request["inputs"].get("native_credentials"):
        try:
            source.write_text("wrong")
        except OSError:
            pass
        else:
            raise RuntimeError("Credential file must be read-only")
        denied = request["inputs"].get("denied_credential_path")
        assert not denied or not Path(denied).exists()
        raw = credential.encode()
        os.write(2, raw[:7])
        time.sleep(0.03)
        os.write(2, raw[7:] + b"\n")
        print(json.dumps({"unexpected_echo": credential}, ensure_ascii=True), file=sys.stderr, flush=True)
        print("review-native-credentials-ok", file=sys.stderr, flush=True)
if request["model"] == "delayed-fixture":
    print("review-provider-waiting", file=sys.stderr, flush=True)
    time.sleep(20)
if request["inputs"].get("native_checks"):
    repository = Path(request["repository"])
    assert (repository / "code.txt").read_text() == "native-git-checkout\n"
    assert request["files"][0]["text"] == "native-git-checkout\n"
    try:
        (repository / "must-not-write").write_text("wrong")
    except OSError:
        pass
    else:
        raise RuntimeError("Review project must be read-only")
    probe = subprocess.run(["/bin/probe", "socket", "2"], capture_output=True, text=True, check=True)
    assert "socket=-1 errno=1" in probe.stdout
    assert not Path(request["inputs"]["control_directory"]).exists()
    print("review-native-boundary-ok", file=sys.stderr, flush=True)
review = {"summary": "Explicit deterministic fixture: " + request["task"],
          "risk": "low", "issues": [], "recommendations": []}
if request["inputs"].get("native_credentials"):
    review["summary"] += " Echo is redacted: " + credential
if request["assess"] == "complexity":
    review["complexity"] = "low"
if request["scope"]:
    review["out_of_scope"] = ["Real model providers and remote credentials are not exercised by this fixture."]
if request["verdict"]:
    review["ready"] = True
print(json.dumps(review))
