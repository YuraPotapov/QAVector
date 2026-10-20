"""Repack only selected public installed-library dependencies for an offline fixture."""

import json
from pathlib import Path
import re
import subprocess
import sys

if __package__:
    from .docker_host import command, local_docker
else:
    from docker_host import command, local_docker


FORMAT = '${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\t${Depends}\t${Pre-Depends}\t${Provides}\n'


def database(raw):
    entries = {}
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 7 or fields[2] not in ("amd64","all") or fields[3] != "installed":
            continue
        name, version, architecture, _, depends, predepends, provides = fields
        entries[name.split(":")[0]] = dict(name=name,version=version,architecture=architecture,
            depends=depends,predepends=predepends,provides=provides)
    return entries


def satisfied(entry, requirement):
    match = re.search(r'\((<<|<=|=|>=|>>)\s*([^\)]+)\)',requirement)
    return entry is not None and (match is None or subprocess.run(["dpkg","--compare-versions",entry["version"],match[1],match[2]],
        capture_output=True).returncode == 0)


def resolve_dependencies(base, host, requirements):
    """Resolve this fixture against installed packages, without a repository."""
    selected = {}
    pending = list(requirements)
    clauses = []
    while pending:
        for requirement in pending.pop().split(","):
            if not requirement.strip():
                continue
            clauses.append(requirement)
            alternatives = requirement.strip().split("|")
            found = False
            for alternative in alternatives:
                name = alternative.strip().split()[0].split(":")[0]
                if satisfied(selected.get(name,base.get(name)),alternative):
                    found = True
                    break
            if found:
                continue
            for alternative in alternatives:
                name = alternative.strip().split()[0].split(":")[0]
                entry = host.get(name)
                if satisfied(entry,alternative):
                    if name in selected and selected[name]["version"] != entry["version"]:
                        raise ValueError("Conflicting offline package versions")
                    selected[name] = entry
                    pending.extend(one for one in (entry["depends"],entry["predepends"]) if one)
                    found = True
                    break
            if not found:
                raise ValueError("No installed/cache baseline candidate for dependency: " + requirement)
    # A selected upgrade replaces the baseline version. Recheck constraints
    # encountered before that replacement instead of silently mixing versions.
    for requirement in clauses:
        if not any(satisfied(selected.get(alternative.strip().split()[0].split(":")[0],
                base.get(alternative.strip().split()[0].split(":")[0])),alternative)
                for alternative in requirement.split("|")):
            raise ValueError("Conflicting offline dependency: " + requirement)
    return selected


def prepare(output, image_id, archives):
    if sys.platform != "linux":
        raise ValueError("Dependency fixture targets Linux; Windows runtime remains a stub")
    output = Path(output)
    if output.exists():
        raise ValueError("Choose a new dependency fixture directory")
    local_docker()
    image_id = command(["docker","image","inspect","--format","{{.Id}}",image_id])
    output.mkdir(parents=True)
    base_raw = command(["docker","run","--rm","--pull=never","--network=none","--read-only","--cap-drop=ALL",
        "--security-opt=no-new-privileges","--memory=256m","--pids-limit=64","--entrypoint=dpkg-query",image_id,"-W","-f=" + FORMAT],strip=False)
    base = database(base_raw)
    host = database(command(["dpkg-query","-W","-f=" + FORMAT],strip=False))
    selected = resolve_dependencies(base,host,
        [command(["dpkg-deb","--field",str(archive),"Depends"]).strip() for archive in archives])
    packages = sorted(entry["name"] for entry in selected.values())
    log = output / "repack.log"
    if packages:
        with log.open("w") as stream:
            subprocess.run(["dpkg-repack","--tag=none","--deb-option=--root-owner-group","--deb-option=-Zgzip","--deb-option=-z1",*packages],
                cwd=output,stdout=stream,stderr=subprocess.STDOUT,check=True)
    report = {"base_image_id":image_id,"origin":"public installed system packages, locally repacked; no repository downloads",
        "selected":selected,"base_packages":{name:entry["version"] for name,entry in base.items()},"network":"none"}
    (output / "dependencies.json").write_text(json.dumps(report,indent=2) + "\n")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--image",required=True,help="Cached base image; never pulled")
    parser.add_argument("archives",nargs="+",type=Path)
    options = parser.parse_args()
    print(json.dumps({"packages":len(prepare(options.output,options.image,options.archives)["selected"])}))
