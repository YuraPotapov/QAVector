"""Secret inputs are explicit, bounded and authorized by the immutable policy."""

from dataclasses import FrozenInstanceError

import pytest

from execution_runtime import PolicyLayer, PolicyPatch, PolicyResolver, RuntimePolicyError, SecretValues
from execution_runtime.backends.base import ExecutionRequest
from execution_runtime.secrets import secret_file_name


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_secret_values_do_not_enter_policy_identity_or_representations(platform):
    policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({"secrets": ["vault/project/key"]}))],
        platform=platform, provider=platform + "-isolated")
    inputs = {"vault/project/key": "synthetic-sensitive-value"}
    request = ExecutionRequest(policy, ("tool",), "workspace", secret_values=inputs)
    inputs["vault/project/key"] = "changed"
    assert request.secret_values.items() == (("vault/project/key", "synthetic-sensitive-value"),)
    assert "synthetic-sensitive-value" not in repr(request) + repr(request.secret_values) + repr(policy.to_document())
    assert ExecutionRequest(policy, ("tool",), "workspace", secret_values={"vault/project/key": "rotated"}).policy.digest == policy.digest
    with pytest.raises(FrozenInstanceError):
        request.secret_values._items = ()


def test_ungranted_secret_is_rejected_without_echoing_the_value():
    policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({}))], platform="linux", provider="linux-isolated")
    with pytest.raises(RuntimePolicyError) as exc:
        ExecutionRequest(policy, ("tool",), "/", secret_values={"not_allowed": "sensitive"})
    assert exc.value.code == "secret_denied" and "sensitive" not in str(exc.value)


@pytest.mark.parametrize("values", [[], {"key": ""}, {"key": b"bytes"}, {"key": "x" * 16385},
                                   {"key": "\ud800"}, {"../bad": "text"},
                                   {"key%d" % i: "text" for i in range(33)}])
def test_invalid_or_excessive_inputs_fail_without_exposing_values(values):
    with pytest.raises(RuntimePolicyError):
        SecretValues(values)


def test_secret_ids_never_become_paths_or_collide_on_namespace_separators():
    assert len(secret_file_name("vault/../key")) == 64
    assert secret_file_name("vault/key") != secret_file_name("vault_key")
