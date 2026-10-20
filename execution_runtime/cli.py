"""JSON diagnostics and profile commands; never executes a workload."""

import argparse
import json
import sys

from ._json import object_fields, read_document
from .backends.registry import BackendRegistry
from .errors import RuntimePolicyError
from .policy import LAYER_ORDER, PolicyLayer, PolicyPatch, PolicyResolver
from .registry import EnvironmentProfile, EnvironmentRegistry
from .deployments import DeploymentRegistry, RuntimeDeployment


def _effective(raw, registry):
    object_fields(raw, ("provider", "layers", "bindings", "environment", "runtime_root_identity"),
                  "request", ("layers",))
    provider = raw.get("provider", "auto")
    reference = raw.get("environment")
    profile = None
    revision = ""
    if reference is not None:
        object_fields(reference, ("name", "revision"), "environment", ("name", "revision"))
        record = registry.load(reference["name"], reference["revision"])
        profile, revision = record.profile, record.revision
        if profile.provider != "auto":
            if provider not in ("auto", profile.provider):
                raise RuntimePolicyError("provider_profile_conflict", "Requested provider conflicts with the pinned profile.")
            provider = profile.provider
    backend = BackendRegistry().select(provider)
    if not isinstance(raw["layers"], list):
        raise RuntimePolicyError("invalid_type", "layers must be an array.", "layers")
    if len(raw["layers"]) > len(LAYER_ORDER):
        raise RuntimePolicyError("invalid_layer_order", "Too many authority layers.", "layers")
    layers = []
    for one in raw["layers"]:
        object_fields(one, ("name", "policy"), "layers", ("name", "policy"))
        layers.append(PolicyLayer(one["name"], PolicyPatch(one["policy"])))
    # Validate caller authority order before inserting a pinned registry profile.
    names = [one.name for one in layers]
    if (not names or names[0] != "platform" or
            [LAYER_ORDER.index(name) for name in names] != sorted(set(LAYER_ORDER.index(name) for name in names))):
        raise RuntimePolicyError("invalid_layer_order", "Start with platform and follow unique authority order.")
    if profile is not None:
        if "environment" in names:
            raise RuntimePolicyError("profile_layer_conflict", "A pinned profile replaces the environment layer.")
        index = next((i for i, one in enumerate(layers) if one.name == "node"), len(layers))
        layers.insert(index, PolicyLayer("environment", profile.policy))
    policy = PolicyResolver().resolve(layers, raw.get("bindings", []),
                                      platform=backend.platform, provider=backend.provider,
                                      profile_revision=revision,
                                      runtime_root_identity=raw.get("runtime_root_identity", ""))
    return policy, backend


def command(kind, name="", source=None, directory=None, revision=None, expected_revision=None, deployments_directory=None):
    """Return (exit code, JSON object), shared by standalone and launcher CLI."""
    try:
        if kind not in ("list", "show", "save", "validate", "inspect", "preflight", "bundle_info",
                        "deployment_list", "deployment_show", "deployment_save", "deployment_draft", "deployment_validate", "deployment_check", "gateway_validate"):
            raise RuntimePolicyError("unknown_command", "Unknown runtime command.")
        if deployments_directory is not None and not kind.startswith("deployment_"):
            raise RuntimePolicyError("invalid_option", "--runtime-deployments-dir applies to deployment and service commands.")
        if kind in ("show", "save", "deployment_show", "deployment_save") and not name:
            raise RuntimePolicyError("missing_profile_name", "The command needs a profile name.")
        if revision is not None and kind not in ("show", "deployment_show"):
            raise RuntimePolicyError("invalid_option", "--runtime-revision applies only to show.")
        if expected_revision is not None and kind not in ("save", "deployment_save"):
            raise RuntimePolicyError("invalid_option", "--runtime-expected-revision applies only to save.")
        if source is not None and kind in ("list", "show", "bundle_info", "deployment_list", "deployment_show"):
            raise RuntimePolicyError("invalid_option", "--from applies only to validate/save/inspect/preflight.")
        registry = EnvironmentRegistry(directory)
        if kind.startswith("deployment_"):
            deployments = DeploymentRegistry(deployments_directory)
            if kind == "deployment_list":
                return 0, {"ok": True, "deployments": [{"name": one.name, "revision": one.digest,
                    "description": one.to_document()["metadata"].get("description", ""),
                    "provider": one.to_document()["spec"]["provider"]} for one in deployments.list()]}
            if kind == "deployment_show":
                one = deployments.load(name, revision)
            else:
                if not source:
                    raise RuntimePolicyError("missing_source", "This deployment command needs --from=FILE.")
                raw = read_document(source)
                if kind == "deployment_check":
                    from .readiness import check
                    return 0, check(raw, deployments, registry)
                if kind == "deployment_draft":
                    one = BackendRegistry().select().deployment_draft(raw)
                else:
                    one = RuntimeDeployment(raw)
                if kind == "deployment_save" and one.name != name:
                    raise RuntimePolicyError("profile_name_mismatch", "Command and deployment name must agree.")
                if kind in ("deployment_draft", "deployment_validate"):
                    backend = one.backend((str(deployments.directory), str(registry.directory)))
                    bindings = one.to_document()["spec"]["bindings"]
                    project = next((binding.get("source") for binding in bindings
                                    if isinstance(binding, dict) and binding.get("name") == "project"), "")
                    effective = one.resolve(backend, project, profiles=registry)
                    return 0, {"ok": True, "revision": one.digest, "deployment": one.to_document(),
                               "effective_policy": effective.to_document(), "enforcement": "not_evaluated"}
                deployments.save(one, expected_revision)
            return 0, {"ok": True, "revision": one.digest, "deployment": one.to_document()}
        if kind == "bundle_info":
            return 0, {"ok": True, "bundle": BackendRegistry().select().bundle_info(), "enforcement": "not_evaluated"}
        if kind == "list":
            return 0, {"ok": True, "environments": [one.to_document() for one in registry.list()]}
        if kind == "show":
            return 0, dict(registry.load(name, revision).to_document(), ok=True)
        if not source:
            raise RuntimePolicyError("missing_source", "This command needs --from=FILE containing JSON.")
        raw = read_document(source)
        if kind == "gateway_validate":
            from .gateway import ProviderGateway
            return 0, {"ok": True, "gateway": ProviderGateway(raw).summary(), "enforcement": "not_evaluated"}
        if kind in ("save", "validate"):
            profile = EnvironmentProfile(raw)
            if kind == "save":
                if profile.name != name:
                    raise RuntimePolicyError("profile_name_mismatch", "The command and profile name must agree.")
                record = registry.save(profile, expected_revision)
                return 0, dict(record.to_document(), ok=True)
            return 0, {"ok": True, "profile": profile.to_document(), "revision": profile.digest,
                       "enforcement": "not_evaluated"}
        effective, backend = _effective(raw, registry)
        payload = {"ok": True, "policy_digest": effective.digest,
                   "effective_policy": effective.to_document(), "enforcement": "not_evaluated"}
        if kind == "preflight":
            report = backend.preflight(effective)
            payload.update(ok=report.ready, preflight=report.to_document(), enforcement="verified" if report.ready else "unavailable")
            return (0 if report.ready else 1), payload
        return 0, payload
    except RuntimePolicyError as exc:
        return 2, {"ok": False, "problems": [exc.to_document()]}
    except OSError:
        return 2, {"ok": False, "problems": [{"code": "registry_io_error", "message": "Cannot access the environment registry.", "field": "", "layer": ""}]}


def emit(code, payload):
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=True, allow_nan=False)
    print()
    return code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's own text may contain arbitrary input. Keep the wire safe.
        raise RuntimePolicyError("invalid_arguments", "Expected exactly one runtime command; see --help.")


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(one.split("=", 1)[0] in ("--runtime-service", "--runtime-service-policy") for one in arguments):
        from .service import main as service_main
        return service_main(arguments)
    parser = _Parser(description="Execution Runtime profiles and diagnostics (no workload launch).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--runtime-list", action="store_true")
    group.add_argument("--runtime-show", metavar="NAME")
    group.add_argument("--runtime-save", metavar="NAME")
    group.add_argument("--runtime-bundle-info", action="store_true")
    group.add_argument("--runtime-deployment-list", action="store_true")
    group.add_argument("--runtime-deployment-show", metavar="NAME")
    group.add_argument("--runtime-deployment-save", metavar="NAME")
    group.add_argument("--runtime-deployment-draft", action="store_true")
    group.add_argument("--runtime-deployment-check", action="store_true")
    group.add_argument("--runtime-deployment-validate", action="store_true")
    group.add_argument("--runtime-gateway-validate", action="store_true")
    for name in ("validate", "inspect", "preflight"):
        group.add_argument("--runtime-" + name, action="store_true")
    parser.add_argument("--from", dest="source", metavar="FILE")
    parser.add_argument("--runtime-profiles-dir", dest="directory")
    parser.add_argument("--runtime-deployments-dir", dest="deployments_directory")
    parser.add_argument("--runtime-revision", dest="revision")
    parser.add_argument("--runtime-expected-revision", dest="expected_revision")
    try:
        flags = [one.split("=", 1)[0] for one in arguments if one.startswith("--")]
        if len(flags) != len(set(flags)):
            raise RuntimePolicyError("invalid_arguments", "Options may only be supplied once.")
        options = vars(parser.parse_args(arguments))
        selected = next(name for name in ("list", "show", "save", "validate", "inspect", "preflight", "bundle_info",
                                         "deployment_list", "deployment_show", "deployment_save", "deployment_draft", "deployment_validate", "deployment_check", "gateway_validate")
                        if options["runtime_" + name] is not None and options["runtime_" + name] is not False)
        name = options["runtime_" + selected] if selected in ("show", "save", "deployment_show", "deployment_save") else ""
        return emit(*command(selected, name, options["source"], options["directory"],
                             options["revision"], options["expected_revision"], options["deployments_directory"]))
    except RuntimePolicyError as exc:
        return emit(2, {"ok": False, "problems": [exc.to_document()]})


if __name__ == "__main__":
    sys.exit(main())
