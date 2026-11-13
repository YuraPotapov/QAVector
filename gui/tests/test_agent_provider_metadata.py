"""A provider the GUI has never heard of works through metadata alone."""

from cms_gui.pages.agentchat import Composer
from cms_gui.pages.agents import ConnectionDialog


KINDS = [{"kind": "future-provider", "name": "Future Agent", "default_binary": "future",
          "modes": ["read", "full"], "plans": False, "efforts": ["careful"],
          "mode_description": "Provider-specific policy from the adapter.",
          "capabilities": {"skills": False}}]


def test_composer_uses_capabilities_without_provider_branches(qapp, dispose):
    composer = Composer()
    composer.set_kinds(KINDS)
    composer.set_connections([{"id": "other", "name": "Other", "kind": "future-provider"}])
    assert not composer.mode.button("Edit").isEnabled()
    assert composer.mode.button("Read").isEnabled()
    assert composer.mode.button("Full").isEnabled()
    assert composer.mode.toolTip() == KINDS[0]["mode_description"]
    assert not composer.plan.isEnabled()
    assert not composer.skills_button.isEnabled()
    assert composer.model.currentText() == "Use Future Agent settings"
    assert composer.effort.findData("careful") >= 0
    dispose(composer)


def test_connection_picker_uses_provider_name(qapp, dispose):
    dialog = ConnectionDialog(None, kinds=KINDS)
    assert dialog.model.currentText() == "Use Future Agent settings"
    dispose(dialog)
