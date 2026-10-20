"""Linux-only defaults for explicit, offline workload-image provisioning."""

from pathlib import Path

from ..._json import object_fields
from ...deployments import RuntimeDeployment
from ...errors import RuntimePolicyError
from ...registry import profile_name


def draft(request):
    object_fields(request, ("name", "description", "project_directory", "image", "project_access", "shell", "claude"),
                  "deployment_draft", ("name", "project_directory", "image"))
    profile_name(request["name"])
    project = request["project_directory"]
    if not isinstance(project, str) or not project.startswith("/") or "\0" in project:
        raise RuntimePolicyError("invalid_path", "Choose an absolute project directory.", "project_directory")
    access = request.get("project_access", "read_only")
    if access not in ("read_only", "read_write"):
        raise RuntimePolicyError("invalid_access", "Choose read-only or read/write project access.")
    import runtime_paths
    spec = {"provider": "linux-isolated", "working_directory": "/workspace",
        "policy": {"filesystem": [{"binding": "project", "access": access}],
                   "resources": {"scratch_bytes": 65536}},
        "bindings": [{"name": "project", "source": project, "target": "/workspace"}],
        "backend_options": {"linux": {"state_directory": str(Path(runtime_paths.user_data_root()) /
            "runtime-state" / request["name"]), "image": request["image"]}}}
    if "shell" in request:
        spec["shell"] = request["shell"]
    if "claude" in request:
        from ...gateway import ProviderGateway, STREAM_CONTRACT
        from ...workers import claude_artifact
        from .filesystem import RuntimeFile, RuntimeImage, target_path
        config = request["claude"]
        object_fields(config, ("interpreter", "executable", "gateway"), "claude", ("interpreter", "executable", "gateway"))
        if access != "read_only" or "shell" in request:
            raise RuntimePolicyError("claude_read_only_required", "Claude environments require read-only project access and no shell override.")
        interpreter, executable = (target_path(config[key]) for key in ("interpreter", "executable"))
        route = ProviderGateway(config["gateway"])
        if route.to_document()["contract"] != STREAM_CONTRACT:
            raise RuntimePolicyError("gateway_stream_contract_required", "Choose a v2 Claude OAuth gateway.")
        entrypoint = "/qavector/workers/claude_review_linux.py"
        image = request["image"]
        object_fields(image, ("identity", "files"), "image", ("identity", "files"))
        if not isinstance(image["files"], list):
            raise RuntimePolicyError("invalid_image", "Runtime files must be a pinned list.")
        entries = []
        for one in image["files"]:
            object_fields(one, ("source", "target", "sha256", "executable"), "image.files", ("source", "target", "sha256", "executable"))
            entries.append(RuntimeFile(**one))
        approved = RuntimeImage(tuple(entries))
        if approved.identity != image["identity"]:
            raise RuntimePolicyError("runtime_image_mismatch", "Deployment image identity differs from its approved files.")
        pinned = {one["target"]:one for one in image["files"]}
        if (any(path not in pinned or pinned[path].get("executable") is not True for path in (interpreter, executable)) or
                pinned.get(entrypoint, {}).get("sha256") != claude_artifact("linux")[1]):
            raise RuntimePolicyError("claude_image_incomplete", "Pin executable interpreter/CLI targets and this core's Claude worker in the reviewed image.")
        reference = route.to_document()["credential"]
        spec["policy"].update(network={"mode":"restricted", "endpoints":[route.endpoint]}, secrets=[reference],
                              resources={"scratch_bytes":134217728})
        spec["provider_gateways"] = {"claude":route.to_document()}
        spec["workers"] = {"agent.review":{"contract":"qavector.agent-review/v1", "transport":"claude_cli",
            "entrypoint":entrypoint, "argv":[interpreter,"-I","-B",entrypoint], "claude":executable,
            "gateway":"claude", "credentials":{"oauth_token":reference}}}
    return RuntimeDeployment({"api_version": "qavector.io/v1", "kind": "RuntimeDeployment",
        "metadata": {"name": request["name"], "description": request.get("description", "")}, "spec": spec})
