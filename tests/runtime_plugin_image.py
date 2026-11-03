"""Curate local SDK/runtime test artifacts into an immutable offline image.

This is acceptance-only curation, not the managed Python plugin installer.
"""

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sysconfig

from execution_runtime.backends.linux import RuntimeImage


def plugin_image(directory, base_image, interpreter):
    targets = {one.target: (one.source, one.target, one.executable) for one in base_image.files}
    native = []
    # asyncio/socket/ssl imports need extensions even though this node performs
    # no socket operations. Every file is pinned, not exposed from host /usr.
    for source in Path(sysconfig.get_config_var("DESTSHARED")).glob("*.so"):
        targets[str(source)] = (str(source), str(source), False)
        native.append(source)
    for name in ("qavector_sdk", "qavector_agent_claude", "jsonschema", "jsonschema_specifications", "referencing", "rpds", "attrs", "attr", "typing_extensions"):
        spec = importlib.util.find_spec(name)
        if spec.submodule_search_locations:
            root = Path(next(iter(spec.submodule_search_locations)))
            files = [(path, "/qavector/site/" + name + "/" + path.relative_to(root).as_posix())
                     for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts
                     and path.suffix != ".pyc"]
        else:
            files = [(Path(spec.origin), "/qavector/site/" + name + ".py")]
        for source, target in files:
            targets[target] = (str(source), target, False)
            if source.suffix == ".so":
                native.append(source)
    for source in native:
        result = subprocess.run(["ldd", str(source)], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            match = re.search(r"(?:=>\s+|^\s*)(/\S+)", line)
            if match and Path(match.group(1)).is_file():
                path = match.group(1)
                targets[path] = (path, path, os.access(path, os.X_OK))
    provider = Path(__file__).parents[1] / "examples/plugin_contracts/count_provider.py"
    targets["/qavector/site/count_provider.py"] = (str(provider), "/qavector/site/count_provider.py", False)
    bootstrap = directory / "plugin_worker.py"
    bootstrap.write_text("import sys\nsys.path.insert(0, '/qavector/site')\nfrom qavector_sdk.worker import main\nraise SystemExit(main())\n")
    target = "/qavector/plugin_worker.py"
    targets[target] = (str(bootstrap), target, False)
    return RuntimeImage.from_files(list(targets.values())), (interpreter, "-I", "-B", target)
