"""The model list is discovered on demand; delayed responses never change a choice."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui.agentmodel import ModelPicker

MODELS = [{"value": "opus", "label": "Opus from CLI", "description": "Complex tasks",
           "resolved_model": "claude-opus-test"},
          {"value": "sonnet", "label": "Sonnet from CLI", "description": "Everyday tasks"}]


@pytest.fixture
def picker(qapp, dispose):
    picker = ModelPicker()
    yield picker
    dispose(picker)


def test_opening_the_list_discovers_models_without_a_text_field(picker):
    calls = []
    picker.requested.connect(calls.append)
    assert not picker.isEditable() and calls == []
    picker.showPopup()
    assert len(calls) == 1
    calls[0]({"ok": True, "models": MODELS})
    picker.setCurrentIndex(picker.findData("sonnet"))
    assert picker.value() == "sonnet" and picker.currentText() == "Sonnet from CLI"
    picker.hidePopup()
    picker.showPopup()
    assert len(calls) == 1
    picker.hidePopup()


def test_a_saved_custom_model_survives_a_new_catalogue(picker):
    picker.set_value("my-gateway-model")
    picker.requested.connect(lambda done: done({"ok": True, "models": MODELS}))
    picker.showPopup()
    assert picker.value() == "my-gateway-model"
    assert "saved" in picker.currentText()
    assert picker.findData("opus") >= 0
    picker.hidePopup()


def test_failure_keeps_the_choice_and_reopening_retries(picker):
    calls = []
    picker.requested.connect(calls.append)
    picker.set_value("sonnet")
    picker.showPopup()
    calls[0]({"ok": False, "error": "Not signed in"})
    assert picker.value() == "sonnet" and "Not signed in" in picker.toolTip()
    picker.hidePopup()
    picker.showPopup()
    assert len(calls) == 2
    picker.hidePopup()


def test_stale_response_from_another_program_is_ignored(picker):
    calls = []
    picker.requested.connect(calls.append)
    picker.set_context(("claude", "/first"))
    picker.showPopup()
    picker.set_context(("claude", "/second"))
    calls[0]({"ok": True, "models": MODELS})
    assert picker.findData("opus") == -1
    picker.hidePopup()


def test_a_choice_made_while_loading_is_kept(picker):
    calls = []
    picker.requested.connect(calls.append)
    picker.set_value("opus")
    picker.showPopup()
    picker.setCurrentIndex(0)
    calls[0]({"ok": True, "models": MODELS})
    assert picker.value() == ""
    picker.hidePopup()
