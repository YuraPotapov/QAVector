"""Offline curation of reviewed installed packages for the Claude gateway image.

The image manifest pins every copied file. This is bundled image preparation,
not a plugin installer or a dependency resolver; nothing is downloaded.
"""

import importlib.util
from pathlib import Path


def package_files():
    for name in ("qavector_sdk", "qavector_agent_claude", "jsonschema",
                 "jsonschema_specifications", "referencing", "rpds", "attrs", "attr", "typing_extensions"):
        spec = importlib.util.find_spec(name)
        if spec is None:
            raise ValueError("Required reviewed image dependency is missing: " + name)
        if spec.submodule_search_locations:
            root = Path(next(iter(spec.submodule_search_locations)))
            for source in sorted(root.rglob("*")):
                if source.is_file() and "__pycache__" not in source.parts and source.suffix != ".pyc":
                    yield source, "/qavector/site/" + name + "/" + source.relative_to(root).as_posix()
        else:
            yield Path(spec.origin), "/qavector/site/" + name + ".py"
