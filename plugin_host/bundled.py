"""Metadata-only catalog for code already shipped in the trusted core build.

The digest covers backend and SDK source/schema resources, not an invented
archive. An external installer must independently verify archive/wheel hashes.
No entrypoint is imported during catalog inspection or registration.
"""

import hashlib
from importlib import resources
import json
import os

from qavector_sdk import ContractError, validate_contract


def registry_path():
    import runtime_paths
    return os.path.join(runtime_paths.user_data_root(), "plugins", "registry.sqlite3")


def claude_manifest():
    manifest = json.loads(resources.files("qavector_agent_claude").joinpath(
        "manifest.json").read_text(encoding="utf-8"))
    validate_contract("qavplug-manifest", manifest)
    return manifest


def _payload(root, prefix):
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name == "__pycache__":
            continue
        name = prefix + "/" + entry.name
        if entry.is_dir():
            yield from _payload(entry, name)
        elif entry.name.endswith((".py", ".json", ".lock")):
            yield name, entry.read_bytes()


def claude_binding():
    manifest = claude_manifest()
    hashes = []
    for package in ("qavector_agent_claude", "qavector_sdk"):
        files = list(_payload(resources.files(package), package))
        # Frozen distributions must ship sources for the same verifiable
        # inventory. Never claim a digest when sources were omitted by a build.
        if not any(name.endswith("/__init__.py") for name, _ in files):
            raise ContractError("PLUGIN_NOT_AVAILABLE", "Bundled source inventory is missing")
        hashes.extend((name, hashlib.sha256(data).hexdigest()) for name, data in files)
    digest = hashlib.sha256(json.dumps(hashes, separators=(",", ":")).encode()).hexdigest()
    return {"plugin_id": manifest["plugin_id"], "version": manifest["version"],
            "digest": digest, "contribution": manifest["contributions"][0]}


def register_claude(catalog):
    manifest, binding = claude_manifest(), claude_binding()
    # Initial activation is in the registration transaction, so simultaneous
    # first runs cannot observe an installed-but-not-yet-active bundle.
    catalog.register(manifest, binding["digest"], publisher_identity="qavector:bundled",
                     bundled=True, activate_initial=True,
                     aliases={"claude_cli": binding["contribution"]["id"]})
    return binding


def verify_claude(binding):
    if binding != claude_binding():
        raise ContractError("PLUGIN_NOT_AVAILABLE", "Exact pinned bundled backend is unavailable in this core build")
