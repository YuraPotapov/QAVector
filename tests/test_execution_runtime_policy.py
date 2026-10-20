"""Adversarial policy resolution checks; no native execution or private paths."""

from dataclasses import FrozenInstanceError, replace

import pytest

from execution_runtime import Policy, PolicyLayer, PolicyPatch, PolicyResolver, RuntimePolicyError
from execution_runtime.paths import PathMapper


def layer(name, **policy):
    return PolicyLayer(name, PolicyPatch(policy))


def resolve(*layers, platform="linux", bindings=None, **kw):
    if bindings is None:
        bindings = [{"name": "project", "source": "/projects/a", "target": "/workspace"}]
    return PolicyResolver().resolve(layers, bindings, platform=platform,
                                    provider=platform + "-isolated", **kw)


def test_default_policy_denies_external_access_and_requires_containment():
    policy = resolve(layer("platform")).policy.to_document()
    assert policy["filesystem"] == policy["tools"] == policy["integrations"] == policy["secrets"] == []
    assert policy["environment"] == []
    assert policy["network"] == {"mode": "none", "endpoints": []}
    assert policy["security"] == {"contain_descendants": True, "privilege_escalation": False}


def test_policy_and_resolution_do_not_alias_mutable_inputs_or_output_views():
    raw = {"tools": ["git"], "resources": {"memory": {"bytes": 2048, "accounting": "resident"}}}
    patch = PolicyPatch(raw)
    raw["tools"].append("shell")
    raw["resources"]["memory"]["bytes"] = 1
    result = resolve(PolicyLayer("platform", patch))
    before = result.digest
    output = result.to_document()
    output["policy"]["tools"].append("shell")
    output["layers"][0]["policy"]["resources"]["memory"]["bytes"] = 1
    output["bindings"][0]["source"] = "/private"
    assert result.digest == before
    assert result.policy.to_document()["tools"] == ["git"]
    assert result.policy.to_document()["resources"]["memory"]["bytes"] == 2048
    with pytest.raises(FrozenInstanceError):
        result.policy._canonical = "{}"


@pytest.mark.parametrize("grant", ["tools", "secrets", "environment"])
def test_explicit_empty_grants_cannot_be_restored_by_a_lower_layer(grant):
    baseline = layer("platform", **{grant: ["allowed"]})
    disabled = layer("cycle", **{grant: []})
    assert resolve(baseline, disabled, layer("node")).policy.to_document()[grant] == []
    with pytest.raises(RuntimePolicyError) as exc:
        resolve(baseline, disabled, layer("node", **{grant: ["allowed"]}))
    assert (exc.value.code, exc.value.layer, exc.value.field) == ("policy_widening", "node", grant)


def test_filesystem_reviewer_can_only_narrow_workspace():
    baseline = layer("platform", filesystem=[{"binding": "project", "access": "read_write"}])
    reviewer = layer("node", filesystem=[{"binding": "project", "path": "src", "access": "read_only"}])
    result = resolve(baseline, reviewer)
    assert result.to_document()["resolved_filesystem"] == [
        {"source": "/projects/a/src", "target": "/workspace/src", "access": "read_only"}]
    with pytest.raises(RuntimePolicyError, match="inherited"):
        resolve(baseline, layer("cycle", filesystem=[{"binding": "project", "access": "read_only"}]),
                layer("node", filesystem=[{"binding": "project", "access": "read_write"}]))


@pytest.mark.parametrize("child", [
    [{"binding": "private", "access": "read_only"}],
    [{"binding": "project", "path": "source-other", "access": "read_only"}],
    [{"binding": "project", "access": "read_write"}],
])
def test_filesystem_rejects_alias_prefix_and_parent_directory_expansion(child):
    with pytest.raises(RuntimePolicyError) as exc:
        resolve(layer("platform", filesystem=[{"binding": "project", "path": "source", "access": "read_only"}]),
                layer("node", filesystem=child))
    assert exc.value.code == "policy_widening"


def test_missing_binding_is_not_interpolated_from_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", "/private/forbidden")
    with pytest.raises(RuntimePolicyError) as exc:
        resolve(layer("platform", filesystem=[{"binding": "unknown", "access": "read_only"}]))
    assert exc.value.code == "missing_binding"


def test_resource_ceilings_keep_stricter_values_and_their_provenance():
    result = resolve(layer("platform", resources={"cpu_cores": 2, "processes": 8,
                         "memory": {"bytes": 2048, "accounting": "resident"}}),
                     layer("project", resources={"cpu_cores": 4, "processes": 4}),
                     layer("node", resources={"memory": {"bytes": 1024, "accounting": "resident"}}))
    assert result.policy.to_document()["resources"] == {
        "cpu_cores": 2, "processes": 4, "memory": {"bytes": 1024, "accounting": "resident"}}
    provenance = result.provenance.to_document()
    assert provenance["resources.cpu_cores"] == "platform"
    assert provenance["resources.processes"] == "project"
    assert provenance["resources.memory"] == "node"


def test_windows_memory_commit_cannot_silently_replace_resident_limit():
    with pytest.raises(RuntimePolicyError) as exc:
        resolve(layer("platform", resources={"memory": {"bytes": 2048, "accounting": "resident"}}),
                layer("node", resources={"memory": {"bytes": 1024, "accounting": "committed"}}))
    assert exc.value.code == "incompatible_accounting"


def test_charged_memory_and_tasks_are_distinct_portable_requirements():
    result = resolve(layer("platform", resources={"tasks": 32,
        "memory": {"bytes": 67108864, "accounting": "charged"}}),
        layer("node", resources={"tasks": 16}))
    assert result.policy.to_document()["resources"]["tasks"] == 16
    assert {"memory_charged", "limit_tasks"} <= set(result.policy.required_capabilities())
    assert "limit_processes" not in result.policy.required_capabilities()
    with pytest.raises(RuntimePolicyError) as exc:
        resolve(layer("platform", resources={"memory": {"bytes": 67108864, "accounting": "charged"}}),
            layer("node", resources={"memory": {"bytes": 33554432, "accounting": "resident"}}))
    assert exc.value.code == "incompatible_accounting"


def test_network_can_be_disabled_but_not_restored_past_an_explicit_denial():
    endpoint = {"host": "git.example.com", "port": 443}
    root = layer("platform", network={"mode": "restricted", "endpoints": [endpoint]})
    assert resolve(root, layer("node", network={"mode": "none"})).policy.to_document()["network"]["mode"] == "none"
    with pytest.raises(RuntimePolicyError) as exc:
        resolve(root, layer("cycle", network={"mode": "none"}),
                layer("node", network={"mode": "restricted", "endpoints": [endpoint]}))
    assert exc.value.code == "policy_widening"


@pytest.mark.parametrize("network", [
    {"mode": "full"},
    {"mode": "restricted", "endpoints": [{"host": "other.example.com", "port": 443}]},
    {"mode": "restricted", "endpoints": [{"host": "git.example.com", "port": 22}]},
])
def test_restricted_network_does_not_allow_other_destinations(network):
    with pytest.raises(RuntimePolicyError):
        resolve(layer("platform", network={"mode": "restricted", "endpoints": [{"host": "git.example.com", "port": 443}]}),
                layer("node", network=network))


def test_integration_actions_and_resources_are_individually_scoped():
    baseline = layer("platform", integrations=[{"service": "jira", "resource": "project:A", "actions": ["read", "transition"]}])
    allowed = {"service": "jira", "resource": "project:A", "actions": ["read"]}
    assert resolve(baseline, layer("node", integrations=[allowed])).policy.to_document()["integrations"] == [allowed]
    for denied in (dict(allowed, resource="project:B"), dict(allowed, actions=["delete"])):
        with pytest.raises(RuntimePolicyError):
            resolve(baseline, layer("node", integrations=[denied]))


@pytest.mark.parametrize("security", [{"contain_descendants": False}, {"privilege_escalation": True}])
def test_node_cannot_weaken_process_security(security):
    with pytest.raises(RuntimePolicyError):
        resolve(layer("platform"), layer("node", security=security))


def test_canonical_hash_ignores_input_order_but_binds_revision_backend_and_paths():
    first = resolve(layer("platform", tools=["python", "git"]))
    second = resolve(layer("platform", tools=["git", "python"]))
    assert first.digest == second.digest
    changed = resolve(layer("platform", tools=["git", "python"]), profile_revision="a" * 64)
    assert first.digest != changed.digest
    windows = resolve(layer("platform", tools=["git", "python"]), platform="windows",
                      bindings=[{"name": "project", "source": "C:/Projects/A", "target": "C:/Work"}])
    assert first.digest != windows.digest


def test_effective_policy_defensively_copies_container_inputs():
    original = resolve(layer("platform"))
    bindings, layers = list(original.bindings), list(original.layers)
    copied = replace(original, bindings=bindings, layers=layers)
    digest = copied.digest
    bindings.clear()
    layers.clear()
    assert copied.digest == digest
    assert copied.bindings == original.bindings and copied.layers == original.layers
    with pytest.raises(RuntimePolicyError):
        replace(original, profile_revision=["mutable"])


def test_numeric_cpu_representation_does_not_change_policy_identity():
    assert Policy({"resources": {"cpu_cores": 2}}).digest == Policy({"resources": {"cpu_cores": 2.0}}).digest


@pytest.mark.parametrize("layers", [[], [layer("node")], [layer("platform"), layer("platform")],
                                      [layer("platform"), layer("node"), layer("project")]])
def test_authority_layers_cannot_be_reordered_or_duplicated(layers):
    with pytest.raises(RuntimePolicyError):
        resolve(*layers)


@pytest.mark.parametrize("document", [
    None, [], {"unexpected": "secret-value"}, {"tools": "git"}, {"tools": ["git", "git"]},
    {"secrets": {"token": "secret-value"}}, {"environment": {"API_KEY": "secret-value"}},
    {"resources": {"processes": True}}, {"resources": {"threads": 1.5}},
    {"resources": {"cpu_cores": float("nan")}}, {"resources": {"cpu_cores": float("inf")}},
    {"resources": {"cpu_cores": 0}}, {"resources": {"scratch_bytes": -1}},
    {"resources": {"processes": 2 ** 64}}, {"resources": {"cpu_cores": 10 ** 400}},
    {"tools": ["tool_" + str(i) for i in range(257)]},
    {"resources": {"cpu_percent": 101}}, {"resources": {"memory": {"bytes": 2, "accounting": "unknown"}}},
    {"security": {"contain_descendants": "true"}}, {"network": {"mode": "none", "endpoints": [{"host": "x", "port": 80}]}},
    {"network": {"mode": "restricted", "endpoints": [{"host": "https://secret-value@host", "port": 443}]}},
    {"network": {"mode": "restricted", "endpoints": [{"host": "*.example.com", "port": 443}]}},
    {"network": {"mode": "restricted", "endpoints": [{"host": "host", "port": True}]}},
    {"filesystem": [{"binding": "project", "path": "../private", "access": "read_only"}]},
    {"filesystem": [{"binding": "project", "path": "x/../private", "access": "read_only"}]},
    {"filesystem": [{"binding": "project", "path": "x\\private", "access": "read_only"}]},
    {"filesystem": [{"binding": "project", "path": "/private", "access": "read_only"}]},
    {"filesystem": [{"binding": "project", "path": "${PROJECT_ROOT}", "access": "read_only"}]},
    {"filesystem": [{"binding": "project", "access": "read_write"}, {"binding": "project", "path": "nested", "access": "read_only"}]},
])
def test_invalid_policy_is_structured_and_does_not_echo_values(document):
    with pytest.raises(RuntimePolicyError) as exc:
        PolicyPatch(document)
    assert "secret-value" not in str(exc.value.to_document())


@pytest.mark.parametrize("path", ["C:relative", "/posix", "C:/", "C:/safe/../private", "C:/safe/file:stream",
                                   "C:/safe/CON.txt", "C:/safe/trailing.", "C:/safe/trailing ",
                                   "//host/share/data", "//?/C:/safe", "//./pipe/control"])
def test_windows_path_rules_are_testable_on_linux_and_fail_closed(path):
    with pytest.raises(RuntimePolicyError):
        PathMapper("windows").absolute(path, "path")


def test_windows_mapping_does_not_use_linux_path_semantics():
    mapper = PathMapper("windows")
    bindings = mapper.bindings([{"name": "project", "source": "C:/Projects/A", "target": "D:/Workspace"}])
    assert mapper.resolve(bindings[0], "src/file.py") == {
        "source": "C:\\Projects\\A\\src\\file.py", "target": "D:\\Workspace\\src\\file.py"}
    with pytest.raises(RuntimePolicyError) as exc:
        mapper.bindings([{"name": "a", "source": "C:/A", "target": "C:/Work"},
                         {"name": "b", "source": "C:/B", "target": "c:/WORK/sub"}])
    assert exc.value.code == "overlapping_targets"
