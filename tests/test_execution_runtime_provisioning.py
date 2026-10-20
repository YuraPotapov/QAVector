"""GUI-facing provisioning never executes workload code or writes before Save."""

import json
import sys

import pytest

from execution_runtime import cli
from execution_runtime.backends.registry import BackendRegistry
from execution_runtime.errors import RuntimePolicyError


@pytest.fixture
def provisioning_request(tmp_path, monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux workload-image provisioning; Windows is a stub")
    from execution_runtime.backends.linux import RuntimeImage
    monkeypatch.setenv("CMS_HOME", str(tmp_path / "control"))
    tool = tmp_path / "reviewed-tool"
    tool.write_bytes(b"reviewed fixture; not executed")
    image = RuntimeImage.from_files([(str(tool), "/bin/reviewed", True)])
    return {"name": "demo", "project_directory": str(tmp_path / "project"),
        "image": {"identity": image.identity, "files": [vars(one) for one in image.files]}}


def command(tmp_path, operation, document, **kwargs):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(document))
    return cli.command(operation, source=source, deployments_directory=tmp_path / "control" / "deployments",
                       directory=tmp_path / "control" / "profiles", **kwargs)


def test_draft_and_validation_resolve_offline_read_only_without_writing(tmp_path, provisioning_request):
    code, result = command(tmp_path, "deployment_draft", provisioning_request)
    assert code == 0 and result["enforcement"] == "not_evaluated"
    assert result["effective_policy"]["policy"]["network"]["mode"] == "none"
    assert result["effective_policy"]["policy"]["filesystem"][0]["access"] == "read_only"
    assert result["deployment"]["spec"]["provider"] == "linux-isolated"
    code, checked = command(tmp_path, "deployment_validate", result["deployment"])
    assert code == 0 and checked["revision"] == result["revision"]
    assert not (tmp_path / "control").exists()


@pytest.mark.parametrize("patch", [{"project_access": "invalid"}, {"project_directory": "relative"},
    {"shell": []}, {"image": {"identity": "sha256:wrong", "files": []}}, {"unexpected": "value"}])
def test_invalid_draft_is_rejected_without_creating_a_registry(tmp_path, provisioning_request, patch):
    provisioning_request.update(patch)
    code, result = command(tmp_path, "deployment_draft", provisioning_request)
    assert code == 2 and result["ok"] is False
    assert not (tmp_path / "control").exists()


def test_draft_is_native_backend_contract_with_explicit_windows_stub():
    backend = BackendRegistry().select("windows-isolated", host="windows")
    with pytest.raises(RuntimePolicyError) as problem:
        backend.deployment_draft({})
    assert problem.value.code == "provisioning_unavailable"


def test_save_requires_reviewed_revision_and_validation_detects_corrupt_image(tmp_path, provisioning_request):
    _, result = command(tmp_path, "deployment_draft", provisioning_request)
    original = result["deployment"]
    code, saved = command(tmp_path, "deployment_save", original, name="demo")
    assert code == 0
    original["metadata"]["description"] = "another editor"
    code, updated = command(tmp_path, "deployment_save", original, name="demo", expected_revision=saved["revision"])
    assert code == 0
    original["metadata"]["description"] = "stale editor"
    code, conflict = command(tmp_path, "deployment_save", original, name="demo", expected_revision=saved["revision"])
    assert code == 2 and conflict["problems"][0]["code"] == "deployment_revision_conflict"
    code, stored = cli.command("deployment_show", "demo", deployments_directory=tmp_path / "control" / "deployments")
    assert code == 0 and stored["revision"] == updated["revision"]
    original["spec"]["backend_options"]["linux"]["image"]["identity"] = "sha256:" + "0" * 64
    code, invalid = command(tmp_path, "deployment_validate", original)
    assert code == 2 and invalid["problems"][0]["code"] == "runtime_image_mismatch"


def test_launcher_cli_accepts_new_commands(tmp_path, provisioning_request, capsys):
    source = tmp_path / "request.json"
    source.write_text(json.dumps(provisioning_request))
    assert cli.main(["--runtime-deployment-draft", "--from=" + str(source)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deployment"]["metadata"]["name"] == "demo"


@pytest.fixture
def claude_request(provisioning_request, tmp_path):
    from execution_runtime.backends.linux import RuntimeImage
    from execution_runtime.workers import claude_artifact
    source, _ = claude_artifact("linux")
    tool = tmp_path / "reviewed-tool"
    image = RuntimeImage.from_files([(str(tool),"/approved/python",True),(str(tool),"/approved/claude",True),
        (str(source),"/qavector/workers/claude_review_linux.py",False)])
    provisioning_request["image"] = {"identity":image.identity,"files":[vars(one) for one in image.files]}
    provisioning_request["claude"] = {"interpreter":"/approved/python","executable":"/approved/claude",
        "gateway":{"contract":"qavector.provider-egress/v2","provider":"claude_cli","authentication":"claude-oauth-token",
            "endpoint":"https://api.anthropic.com/v1/messages","addresses":["1.1.1.1"],"models":["approved-full-model"],"credential":"TEST_OAUTH"}}
    return provisioning_request


def test_claude_draft_never_reads_auth_contacts_provider_or_changes_existing_config(tmp_path, claude_request, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No credentials or outbound traffic during draft/validation/Save")
    monkeypatch.setattr("cycle.secrets.load",forbidden)
    monkeypatch.setattr("execution_runtime.gateway._PinnedHTTPS",forbidden)
    monkeypatch.setattr("cycle.plugins.agent_cli.auth_status",forbidden)
    code, result = command(tmp_path,"deployment_draft",claude_request)
    assert code == 0, result
    spec = result["deployment"]["spec"]
    assert spec["workers"]["agent.review"]["transport"] == "claude_cli"
    assert spec["policy"]["secrets"] == ["TEST_OAUTH"] and spec["policy"]["resources"]["scratch_bytes"] == 134217728
    assert result["effective_policy"]["policy"]["filesystem"][0]["access"] == "read_only"
    assert result["effective_policy"]["policy"]["network"]["mode"] == "restricted"
    assert not (tmp_path / "control").exists()
    assert command(tmp_path,"deployment_save",result["deployment"],name="demo")[0] == 0
    assert not (tmp_path / "control" / "cycles").exists()


@pytest.mark.parametrize("change", ["write","shell","missing-cli","old-contract","wrong-artifact","malformed-image","unapproved-ip"])
def test_claude_draft_fails_closed_for_incomplete_or_unapproved_input(tmp_path, claude_request, change):
    if change == "write":
        claude_request["project_access"] = "read_write"
    elif change == "shell":
        claude_request["shell"] = ["/bin/sh","-c"]
    elif change == "missing-cli":
        claude_request["claude"]["executable"] = "/unapproved/claude"
    elif change == "old-contract":
        claude_request["claude"]["gateway"]["contract"] = "qavector.provider-egress/v1"
    elif change == "wrong-artifact":
        claude_request["image"]["files"][-1]["sha256"] = "0" * 64
    elif change == "malformed-image":
        claude_request["image"] = None
    else:
        claude_request["claude"]["gateway"]["addresses"] = ["169.254.169.254"]
    code, result = command(tmp_path,"deployment_draft",claude_request)
    assert code == 2 and result["ok"] is False
    assert not (tmp_path / "control").exists()


@pytest.mark.parametrize("change", ["model","null-model","worker","write","scratch","network"])
def test_readiness_denies_before_native_launch_or_secret_access(tmp_path, claude_request, monkeypatch, change):
    _, draft = command(tmp_path,"deployment_draft",claude_request)
    payload = {"deployment":draft["deployment"],"worker":"agent.review","model":"approved-full-model"}
    if change == "model": payload["model"] = "unapproved-model"
    elif change == "null-model": payload["model"] = None
    elif change == "worker": payload["worker"] = "command.shell"
    elif change == "write": payload["deployment"]["spec"]["policy"]["filesystem"][0]["access"] = "read_write"
    elif change == "scratch": payload["deployment"]["spec"]["policy"]["resources"]["scratch_bytes"] = 4096
    else: payload["deployment"]["spec"]["policy"]["network"] = {"mode":"none"}
    def forbidden(*args, **kwargs):
        pytest.fail("Rejected readiness must not launch a native workload, read secrets or open provider transport")
    monkeypatch.setattr("execution_runtime.backends.linux.backend.LinuxBackend.gateway_preflight",forbidden)
    monkeypatch.setattr("cycle.secrets.load",forbidden)
    monkeypatch.setattr("execution_runtime.gateway._PinnedHTTPS",forbidden)
    code, result = command(tmp_path,"deployment_check",payload)
    assert code == 2 and result["ok"] is False
    assert not (tmp_path / "control").exists()
