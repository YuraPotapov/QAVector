"""Is this frozen core healthy? Reads its own --describe output.

    python3 packaging/check_frozen.py <describe.json> <expected-version> [<source-describe.json>]

Cheap, and it catches the two failures that are otherwise only visible to the
person who installs the package: a lazy import the spec did not list, and a
resource that did not make it into the bundle. The Windows build first shipped
without its splash for exactly the second reason, which is why this lives in one
file both builds call rather than inline in each of them.

Given the source tree's own --describe too, it also checks that the bundle
carries the cycle plugins as that tree has them - a field added to a plugin and
used by a cycle is otherwise found only when the installed application refuses
the cycle.

Exits non-zero, with the problems named, if anything is wrong.
"""

import json
import sys


def problems(payload, expected_version, source=None):
    """Everything wrong with this bundle, in the order worth reading."""
    found = []
    if payload.get("version") != expected_version:
        found.append("version is %r, expected %r"
                     % (payload.get("version"), expected_version))
    if not payload.get("scenarios"):
        found.append("no scenarios: the flows tree did not make it into the bundle "
                     "(or pyyaml is missing)")
    if not payload.get("extensions"):
        found.append("no extensions: the extensions tree did not make it into the "
                     "bundle")
    for warning in payload.get("warnings", []):
        if "unavailable" in warning:
            found.append(warning)
    if source is not None:
        found.extend(_plugin_drift(payload, source))
    return found


def _plugin_drift(payload, source):
    """Cycle plugins whose fields differ between the bundle and the source."""
    frozen = {one.get("id"): one for one in payload.get("cycle_plugins") or []}
    found = []
    for one in source.get("cycle_plugins") or []:
        there = frozen.get(one.get("id"))
        if there is None:
            found.append("cycle plugin %s is not in the bundle" % one.get("id"))
            continue
        for part in ("inputs", "outputs"):
            want = [field.get("key") for field in one.get(part) or []]
            have = [field.get("key") for field in there.get(part) or []]
            missing = [key for key in want if key not in have]
            if missing:
                found.append("cycle plugin %s in the bundle lacks %s %s: it was "
                             "frozen from another tree"
                             % (one.get("id"), part, ", ".join(missing)))
    return found


def _load(path):
    # utf-8-sig, not utf-8: a describe.json written by a Windows shell may carry
    # a BOM, which json.load() refuses. Reading one is harmless where there
    # isn't one.
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def main(argv):
    if len(argv) not in (3, 4):
        sys.exit(__doc__)
    payload = _load(argv[1])
    found = problems(payload, argv[2], _load(argv[3]) if len(argv) == 4 else None)
    if found:
        sys.exit("frozen core is not healthy:\n  " + "\n  ".join(found))
    print("  %d scenarios, %d extensions, chrome: %s"
          % (len(payload["scenarios"]), len(payload["extensions"]),
             payload.get("chrome", {}).get("path") or "not found"))


if __name__ == "__main__":
    main(sys.argv)
