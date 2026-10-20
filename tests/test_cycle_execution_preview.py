"""Approval input previews are bounded and exclude values declared secret."""

from types import SimpleNamespace

from cycle.execution_policy import input_preview


def test_preview_redacts_secret_fields_references_and_embedded_secret_values():
    context = SimpleNamespace(secrets={"jira": "sensitive-value"}, cycle=SimpleNamespace(secrets=("jira",)))
    raw = {"site": "https://example.test", "token": "${vars.jira}", "command": "echo ${vars.jira}",
           "nested": {"message": "sensitive-value in a result", "directory": "/tmp/task"}, "env_input": "${env.DB_PASSWORD}"}
    resolved = dict(raw, token="sensitive-value", command="echo sensitive-value", env_input="external-password")
    shown = input_preview(resolved, raw, context)
    assert shown["site"] == raw["site"] and shown["nested"]["directory"] == "/tmp/task"
    assert shown["token"] == shown["command"] == shown["env_input"] == "[redacted]"
    assert shown["nested"]["message"] == "[redacted] in a result"
    assert "sensitive-value" not in str(shown) and "external-password" not in str(shown)


def test_preview_bounds_large_inputs_without_changing_the_actual_settings():
    context = SimpleNamespace(secrets={}, cycle=SimpleNamespace(secrets=()))
    settings = {"text": "x" * 2000, "rows": list(range(100))}
    shown = input_preview(settings, settings, context)
    assert len(shown["text"]) == 501 and len(shown["rows"]) == 10
    assert len(settings["text"]) == 2000 and len(settings["rows"]) == 100
