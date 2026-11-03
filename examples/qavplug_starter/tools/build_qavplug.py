"""Build the starter's single-target archive from explicitly prepared wheels.

No plugin module is imported and no installation/build hook is executed here.
The QAVector inspector remains authoritative for package and wheel validation.
"""

import argparse
import base64
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

from packaging.utils import canonicalize_name, parse_wheel_filename


ROOT = Path(__file__).resolve().parents[1]


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_member(root, name):
    relative = PurePosixPath(name)
    if (relative.is_absolute() or ".." in relative.parts or "\\" in name
            or str(relative) != name or not relative.parts):
        raise ValueError("Invalid package member: " + name)
    source = root / name
    if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
        raise ValueError("Package members must be regular files inside the project")
    return source.read_bytes()


def build(root, wheelhouse, output, sign_key=None, key_id=None):
    manifest_raw = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    targets = manifest["runtime"]["targets"]
    if len(targets) != 1:
        raise ValueError("This starter builder requires exactly one target; build its wheelhouse explicitly")
    if manifest["dependencies"]["python_lock"] != "requirements.lock":
        raise ValueError("This starter uses the JSON requirements.lock filename")
    if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", manifest["plugin_id"]):
        raise ValueError("Use a dotted lowercase plugin ID")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", manifest["version"]):
        raise ValueError("Use a stable major.minor.patch package version")
    files = {"manifest.json": manifest_raw}
    for contribution in manifest["contributions"]:
        for key in ("config_schema", "input_schema", "output_schema", "ui_schema"):
            if key in contribution:
                name = contribution[key]
                if not name.startswith("schemas/"):
                    raise ValueError("Referenced schemas must live under schemas/")
                files[name] = read_member(root, name)
    for name in ("README.md", "LICENSE"):
        if (root / name).is_file():
            files[name] = read_member(root, name)
    for folder in ("assets", "licenses"):
        for source in sorted((root / folder).rglob("*")):
            if source.is_file():
                name = source.relative_to(root).as_posix()
                files[name] = read_member(root, name)
    locked, versions = [], {}
    for source in sorted(wheelhouse.glob("*.whl")):
        if source.is_symlink():
            raise ValueError("Wheel inputs must be regular files")
        name, version, _build, _tags = parse_wheel_filename(source.name)
        if name in versions or name == "qavector-sdk":
            raise ValueError("Use one wheel per distribution; the SDK is supplied by QAVector")
        raw = source.read_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
            metadata_paths = [path for path in wheel.namelist() if path.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise ValueError("Wheel needs exactly one METADATA file")
            metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
            if canonicalize_name(metadata["Name"]) != name or metadata["Version"] != str(version):
                raise ValueError("Wheel filename/metadata identity mismatch")
        member = "wheels/" + source.name
        files[member] = raw
        versions[name] = str(version)
        locked.append({"name": str(name), "version": str(version), "file": member, "sha256": digest(raw)})
    own = canonicalize_name(manifest["runtime"]["distribution"])
    if versions.get(own) != manifest["version"]:
        raise ValueError("Build the plugin wheel first; its version must equal manifest.version")
    files["requirements.lock"] = json_bytes({"format_version": 1, "targets": {targets[0]: locked}})
    files["checksums.json"] = json_bytes({name: digest(raw) for name, raw in files.items()})
    if sign_key:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        if not key_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key_id):
            raise ValueError("Signing requires a valid --key-id")
        key = load_pem_private_key(sign_key.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Select an Ed25519 PEM private key")
        signature = key.sign(b"QAVPLUG-SIGNATURE-V1\n" + files["checksums.json"])
        files["signature.json"] = json_bytes({"algorithm": "Ed25519", "key_id": key_id,
                                              "signature": base64.b64encode(signature).decode("ascii")})
    elif key_id:
        raise ValueError("--key-id needs --sign-key")
    output = output or root / "dist" / (manifest["plugin_id"] + "-" + manifest["version"] + ".qavplug")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    print(json.dumps({"archive": str(output.resolve()), "sha256": digest(output.read_bytes()),
                      "signed": bool(sign_key)}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, default=ROOT / "dist/wheels")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sign-key", type=Path)
    parser.add_argument("--key-id")
    args = parser.parse_args()
    build(ROOT, args.wheelhouse, args.output, args.sign_key, args.key_id)


if __name__ == "__main__":
    main()
