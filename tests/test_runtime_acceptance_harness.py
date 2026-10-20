"""Acceptance tooling refuses remote/foreign resources and preserves evidence."""

import importlib
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux acceptance; Windows remains a stub")


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    return importlib.import_module("acceptance.docker_host")


@pytest.mark.parametrize("endpoint", ["tcp://localhost:2375", "ssh://runner", "npipe:////./pipe/docker_engine"])
def test_remote_docker_is_rejected_before_any_command(harness, monkeypatch, endpoint):
    monkeypatch.setenv("DOCKER_HOST", endpoint)
    monkeypatch.setattr(harness, "command", lambda *a, **kw: pytest.fail("No remote Docker access"))
    with pytest.raises(ValueError, match="local Unix"):
        harness.local_docker()


def test_remote_context_is_rejected(harness, monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(harness, "command", lambda *a, **kw: "ssh://remote-runner")
    with pytest.raises(ValueError, match="local Unix"):
        harness.local_docker()


def test_cleanup_checks_ownership_and_attempts_remaining_containers(harness, monkeypatch):
    removed = []
    def command(args):
        if args[1] == "inspect":
            return "foreign-token" if args[-1] == "foreign" else "owned-token"
        removed.append(args[-1])
        if args[-1] == "failed-removal":
            raise subprocess.CalledProcessError(1, args)
        return ""
    monkeypatch.setattr(harness, "command", command)
    result = harness.remove_owned(["anchor", "foreign", "failed-removal", "runner"], "owned-token")
    assert removed == ["runner", "failed-removal", "anchor"]
    assert result["removed"] == ["runner", "anchor"]
    assert {entry["container"] for entry in result["errors"]} == {"foreign", "failed-removal"}


def test_timeout_preserves_bytes_output_for_diagnostics(harness, monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output=b"last successful stage\n", stderr=b"failure detail\n")
    monkeypatch.setattr(harness.subprocess, "run", timeout)
    log = tmp_path / "runner.log"
    with pytest.raises(subprocess.TimeoutExpired):
        harness.run_attached("owned-fixture", log, 1)
    assert "last successful stage\nfailure detail\n" in log.read_text()


@pytest.mark.parametrize("name", ["accept_runtime_cpu", "accept_runtime_install"])
def test_existing_acceptance_output_is_never_touched(harness, monkeypatch, tmp_path, name):
    module = importlib.import_module(name)
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "reviewed-evidence"
    marker.write_bytes(b"keep")
    monkeypatch.setattr(module, "local_docker", lambda: pytest.fail("No containers before output guard"))
    with pytest.raises(ValueError, match="new acceptance output"):
        if name == "accept_runtime_cpu":
            module.accept(tmp_path, output, "fixture")
        else:
            module.accept(tmp_path, tmp_path, tmp_path, output, "fixture")
    assert marker.read_bytes() == b"keep"


@pytest.fixture
def dependencies(harness):
    return importlib.import_module("acceptance.prepare_dependencies")


def entry(name, version="1", depends="", predepends=""):
    return dict(name=name, version=version, architecture="amd64", depends=depends, predepends=predepends, provides="")


def test_dependency_metadata_ignores_uninstalled_and_foreign_architecture(dependencies):
    raw = ("libok:amd64\t1\tamd64\tinstalled\t\t\t\n"
        "libremoved\t1\tamd64\tconfig-files\t\t\t\n"
        "libforeign\t1\tarm64\tinstalled\t\t\t\n")
    assert set(dependencies.database(raw)) == {"libok"}


def test_last_metadata_record_keeps_empty_trailing_columns(harness, dependencies, monkeypatch):
    raw = "last-library\t1\tamd64\tinstalled\t\t\t\n"
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=raw))
    result = dependencies.database(harness.command(["public-package-index"], strip=False))
    assert result["last-library"]["depends"] == ""
    assert result["last-library"]["predepends"] == ""


def test_offline_resolution_prefers_baseline_and_handles_cycles_and_predepends(dependencies):
    base = {"libbase": entry("libbase", "2")}
    host = {"libfirst": entry("libfirst", depends="libsecond, unavailable | libbase (>= 2)"),
        "libsecond": entry("libsecond", predepends="libfirst")}
    selected = dependencies.resolve_dependencies(base, host, ["libfirst"])
    assert set(selected) == {"libfirst", "libsecond"}
    with pytest.raises(ValueError, match="No installed/cache"):
        dependencies.resolve_dependencies(base, host, ["libbase (>= 3)"])


def test_conflicting_upgrade_cannot_reuse_removed_baseline_version(dependencies):
    base = {"libbase": entry("libbase", "1")}
    host = {"libbase": entry("libbase", "2")}
    # The exact baseline version is initially satisfied, but the later clause
    # requires replacing it. One real dpkg database cannot contain both.
    with pytest.raises(ValueError, match="Conflicting offline"):
        dependencies.resolve_dependencies(base, host, ["libbase (>= 2)", "libbase (= 1)"])
