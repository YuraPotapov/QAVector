"""JSON round trips and launcher integration without launching workloads."""

import json
import subprocess
import sys

import pytest

from execution_runtime import cli
from execution_runtime.backends.registry import host_platform


def write(tmp_path, document):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def profile_document():
    return {"api_version": "qavector.io/v1", "kind": "ExecutionEnvironment",
            "metadata": {"name": "development"},
            "spec": {"provider": "auto", "policy": {"network": {"mode": "none"}}}}


def request_document():
    return {"layers": [{"name": "platform", "policy": {"tools": ["git"], "network": {"mode": "full"}}},
                       {"name": "node", "policy": {"tools": []}}]}


def test_profile_save_list_show_and_validate_round_trip(tmp_path):
    source = write(tmp_path, profile_document())
    directory = tmp_path / "registry"
    code, valid = cli.command("validate", source=source, directory=directory)
    assert code == 0 and valid["enforcement"] == "not_evaluated"
    assert not directory.exists()
    code, saved = cli.command("save", "development", source, directory)
    assert code == 0
    assert saved["revision"] == valid["revision"]
    code, listed = cli.command("list", directory=directory)
    assert code == 0 and len(listed["environments"]) == 1
    code, shown = cli.command("show", "development", directory=directory, revision=saved["revision"])
    assert code == 0 and shown == saved


def test_pinned_profile_is_resolved_before_node_without_overriding_ceiling(tmp_path):
    directory = tmp_path / "registry"
    _, saved = cli.command("save", "development", write(tmp_path, profile_document()), directory)
    document = request_document()
    document["environment"] = {"name": "development", "revision": saved["revision"]}
    code, payload = cli.command("inspect", source=write(tmp_path, document), directory=directory)
    assert code == 0
    resolved = payload["effective_policy"]
    assert resolved["profile_revision"] == saved["revision"]
    assert resolved["policy"]["network"]["mode"] == "none"
    assert resolved["policy"]["tools"] == []
    assert [one["name"] for one in resolved["layers"]] == ["platform", "environment", "node"]
    document["layers"][-1]["policy"]["network"] = {"mode": "full"}
    code, payload = cli.command("inspect", source=write(tmp_path, document), directory=directory)
    assert code == 2 and payload["problems"][0]["code"] == "policy_widening"


def test_preflight_reports_unavailable_not_a_successful_sandbox(tmp_path):
    code, result = cli.command("preflight", source=write(tmp_path, request_document()), directory=tmp_path / "registry")
    assert code == 1 and result["ok"] is False
    assert result["enforcement"] == "unavailable"
    assert result["preflight"]["ready"] is False
    assert result["preflight"]["policy_digest"] == result["policy_digest"]
    assert result["preflight"]["capabilities"]["platform"] == host_platform()


@pytest.mark.parametrize("document,code", [
    ({"layers": []}, "invalid_layer_order"),
    ({"layers": [{"name": "node", "policy": {}}]}, "invalid_layer_order"),
    ({"layers": [{"name": "platform", "policy": {}}, {"name": "platform", "policy": {}}]}, "invalid_layer_order"),
    ({"layers": [{"name": "platform", "policy": {}}], "environment": {"name": "development"}}, "missing_field"),
    ({"layers": [{"name": "platform", "policy": {}}], "unexpected": "secret-value"}, "unknown_field"),
    ({"layers": [{"name": "platform", "policy": {"secrets": {"API_KEY": "secret-value"}}}]}, "invalid_type"),
])
def test_invalid_request_has_safe_structured_errors(tmp_path, document, code):
    status, payload = cli.command("inspect", source=write(tmp_path, document), directory=tmp_path / "registry")
    assert status == 2 and payload["problems"][0]["code"] == code
    assert "secret-value" not in json.dumps(payload)


@pytest.mark.parametrize("args", [[], ["--runtime-no-such-command=secret-value"],
                                   ["--runtime-list", "--runtime-validate"], ["--runtime-show"],
                                   ["--runtime-show=first", "--runtime-show=second"]])
def test_standalone_argument_errors_are_json_without_raw_input(args, capsys):
    assert cli.main(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["problems"][0]["code"] == "invalid_arguments"
    assert "secret-value" not in json.dumps(payload)


def test_launcher_forwards_runtime_commands_without_loading_cycle_or_browser(tmp_path):
    script = "import sys; import session_launcher; sys.argv=['session_launcher.py','--runtime-list','--runtime-profiles-dir=' + sys.argv[1]]; session_launcher.main()"
    done = subprocess.run([sys.executable, "-c", script, str(tmp_path / "registry")],
                          capture_output=True, text=True, check=True)
    assert json.loads(done.stdout) == {"ok": True, "environments": []}
    assert not (tmp_path / "registry").exists()


@pytest.mark.parametrize("other", ["--describe", "--cycle-run=demo", "--run-tests=demo", "--flow-list", "--unknown", "--env=demo"])
def test_launcher_refuses_runtime_and_other_execution_commands(tmp_path, other):
    done = subprocess.run([sys.executable, "session_launcher.py", "--runtime-list",
                           "--runtime-profiles-dir=" + str(tmp_path / "registry"), other],
                          capture_output=True, text=True)
    assert done.returncode == 2
    assert json.loads(done.stdout)["problems"][0]["code"] == "conflicting_commands"
