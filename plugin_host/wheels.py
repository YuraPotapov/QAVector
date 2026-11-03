"""Offline wheel installation plan. No pip, build hooks, imports or downloads."""

import base64
import csv
from email.parser import BytesParser
import hashlib
import io

from packaging.requirements import Requirement, InvalidRequirement
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.tags import parse_tag
from packaging.utils import canonicalize_name, parse_wheel_filename, InvalidWheelFilename
from packaging.version import Version, InvalidVersion

from .archive import LIMITS, reject, safe_path, sha256, zip_files


def wheel(raw, filename, runtime):
    try:
        name, version, _, tags = parse_wheel_filename(filename)
        supported = {tag for text in runtime['tags'] for tag in parse_tag(text)}
        if not tags.intersection(supported):
            reject('Wheel is incompatible with ' + runtime['target'] + ': ' + filename, 'PLUGIN_RUNTIME_INCOMPATIBLE')
    except (InvalidWheelFilename, ValueError):
        reject('Invalid wheel filename')
    files = zip_files(raw)
    infos = {path.split('/')[0] for path in files if path.split('/')[0].endswith('.dist-info')}
    if len(infos) != 1:
        reject('Wheel must contain one dist-info directory')
    info = infos.pop()
    stem = info[:-10]
    dist_name, dist_version = stem.rsplit('-', 1)
    if canonicalize_name(dist_name) != name:
        reject('Wheel dist-info name mismatch')
    required = [info + '/' + path for path in ('METADATA', 'WHEEL', 'RECORD')]
    if not set(required) <= set(files):
        reject('Incomplete wheel metadata')
    if any(len(files[path]) > 2 * 1024 ** 2 for path in required[:2]):
        reject('Wheel metadata exceeds 2 MiB')
    metadata, layout = (BytesParser().parsebytes(files[path]) for path in required[:2])
    try:
        if (canonicalize_name(metadata['Name'] or '') != name or Version(metadata['Version'] or '') != version
                or Version(dist_version) != version
                or layout['Wheel-Version'] != '1.0' or layout['Root-Is-Purelib'] not in ('true', 'false')):
            reject('Wheel identity or format mismatch')
        if metadata['Requires-Python'] and not SpecifierSet(metadata['Requires-Python']).contains(runtime['python_version']):
            reject('Wheel requires another Python version: ' + filename, 'PLUGIN_RUNTIME_INCOMPATIBLE')
        declared_tags = {tag for text in layout.get_all('Tag', []) for tag in parse_tag(text)}
        if declared_tags != tags:
            reject('Wheel filename and metadata tags differ')
    except (ValueError, TypeError, InvalidVersion, InvalidSpecifier):
        reject('Invalid wheel metadata')
    records = {}
    try:
        for row in csv.reader(io.StringIO(files[required[2]].decode('utf-8'))):
            if len(row) != 3 or row[0] in records:
                reject('Invalid or duplicate wheel RECORD row')
            records[safe_path(row[0])] = row[1:]
    except (UnicodeError, csv.Error):
        reject('Invalid wheel RECORD')
    if set(records) != set(files):
        reject('Wheel RECORD must cover every file')
    for path, data in files.items():
        hashed, size = records[path]
        if path == required[2]:
            if hashed or size:
                reject('RECORD must not hash itself')
        else:
            if not hashed.startswith(('sha256=', 'sha384=', 'sha512=')):
                reject('Wheel RECORD requires SHA-256 or stronger')
            algorithm, encoded = hashed.split('=', 1)
            actual = base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).rstrip(b'=').decode()
            if actual != encoded or size != str(len(data)):
                reject('Wheel RECORD hash/size mismatch')
    installed = {}
    for path, data in files.items():
        target = path
        if path.split('/')[0].endswith('.data'):
            parts = path.split('/')
            if len(parts) < 3 or parts[0] != info[:-10] + '.data' or parts[1] not in ('purelib', 'platlib'):
                reject('Unsupported wheel install scheme; only purelib/platlib are allowed')
            target = '/'.join(parts[2:])
        parts = target.split('/')
        if (target.endswith(('.pth', '.pyc', '.pyo')) or '__pycache__' in parts
                or parts[0].split('.')[0] in ('qavector_sdk', 'sitecustomize', 'usercustomize')):
            reject('Wheel contains interpreter startup hooks or overrides the SDK')
        if target in installed:
            reject('Wheel install scheme collision')
        installed[target] = data
    return str(name), str(version), metadata.get_all('Requires-Dist', []), installed


def environment(package, runtime):
    manifest = package.manifest
    try:
        if runtime['target'] not in manifest['runtime']['targets'] or not SpecifierSet(
                manifest['runtime']['python']).contains(runtime['python_version']):
            reject('Package does not support runtime target ' + runtime['target'], 'PLUGIN_RUNTIME_INCOMPATIBLE')
    except InvalidSpecifier:
        reject('Invalid runtime Python requirement')
    wheels = package.lock['targets'][runtime['target']]
    if not isinstance(wheels, list) or not wheels:
        reject('Target requires a nonempty wheel lock')
    installed, selected, dependencies, folded = {}, {}, {}, set()
    for item in wheels:
        if not isinstance(item, dict) or set(item) != {'name', 'version', 'file', 'sha256'}:
            reject('Lock entries require name, version, file and sha256')
        path = item['file']
        if not isinstance(path, str) or path not in package.files or not path.startswith('wheels/'):
            reject('Locked wheel is missing for ' + runtime['target'])
        if sha256(package.files[path]) != item['sha256']:
            reject('Locked wheel digest mismatch')
        name, version, requires, files = wheel(package.files[path], path.split('/')[-1], runtime)
        if name != item['name'] or version != item['version'] or name in selected or name == 'qavector-sdk':
            reject('Lock distribution identity conflict')
        selected[name], dependencies[name] = version, requires
        for target, data in files.items():
            if target.casefold() in folded:
                reject('Wheels install colliding paths')
            folded.add(target.casefold())
            installed[target] = data
        if len(installed) > LIMITS.files or sum(map(len, installed.values())) > LIMITS.expanded:
            reject('Combined wheel environment exceeds limits')
    # SDK comes from the pinned runtime; other dependencies must be bundled.
    available = dict(selected, **{'qavector-sdk': runtime['sdk_version']})
    for name, requires in dependencies.items():
        for text in requires:
            try:
                requirement = Requirement(text)
                if requirement.url or requirement.extras:
                    reject('Dependency URLs/extras are unsupported; bundle an explicit lock')
                if requirement.marker and not requirement.marker.evaluate(dict(runtime['markers'], extra='')):
                    continue
                dep = canonicalize_name(requirement.name)
                if dep not in available or not requirement.specifier.contains(available[dep]):
                    reject('Missing/incompatible locked dependency: ' + name + ' -> ' + dep)
            except (InvalidRequirement, ValueError):
                reject('Invalid wheel dependency metadata')
    if canonicalize_name(manifest['runtime']['distribution']) not in selected:
        reject('Plugin distribution is missing from the selected wheel lock')
    paths = set(installed)
    for item in manifest['contributions']:
        module = item['entrypoint'].split(':')[0].replace('.', '/')
        if module + '.py' not in paths and module + '/__init__.py' not in paths:
            reject('Entrypoint module is absent from bundled wheels: ' + item['id'])
    for path in paths:
        parts = path.casefold().split('/')
        if any('/'.join(parts[:n]) in folded for n in range(1, len(parts))):
            reject('Installed file/directory collision')
    return installed
