"""Deterministic wheels/packages built as data; no setuptools hooks."""

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile


def zipped(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, value)
    return stream.getvalue()


def wheel(name='demo', version='1.0.0', code='class Provider:\n    pass\n', extra=None, requires=()):
    info = name + '-' + version + '.dist-info'
    files = {name + '.py': code.encode(),
             info + '/METADATA': ('Metadata-Version: 2.1\nName: ' + name + '\nVersion: ' + version + '\n' +
                                   ''.join('Requires-Dist: ' + item + '\n' for item in requires)).encode(),
             info + '/WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    files.update(extra or {})
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    for path, data in files.items():
        writer.writerow([path, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(), len(data)])
    writer.writerow([info + '/RECORD', '', ''])
    files[info + '/RECORD'] = output.getvalue().encode()
    return zipped(files)


def runtime_document():
    return dict(target='linux-x86_64-cp310', python_version='3.10.12', sdk_version='1.0.0',
                tags=['py3-none-any'], markers={'python_version': '3.10', 'sys_platform': 'linux'})


def package_files(version='1.0.0', kind='node_provider', code=None, permissions=None, wheels=None):
    manifest = json.loads((Path(__file__).parents[1] / 'examples/plugin_contracts/node-manifest.json').read_text())
    manifest.update(plugin_id='example.demo', version=version)
    manifest['runtime'].update(distribution='demo', python='>=3.10', targets=['linux-x86_64-cp310'])
    contribution = dict(kind=kind, id='example.demo.run', entrypoint='demo:Provider', config_schema='schemas/config.json')
    if kind == 'node_provider':
        contribution.update(input_schema='schemas/input.json', output_schema='schemas/output.json')
    else:
        manifest['compatibility']['agent_api'] = '>=1.0.0,<2.0.0'
    manifest['contributions'] = [contribution]
    manifest['permissions_requested'] = permissions or dict(workspace='none', process=[], network='none', credentials=[])
    manifest['external_requirements'] = []
    filename = 'wheels/demo-' + version + '-py3-none-any.whl'
    wheels = wheels or {filename: wheel(version=version, **({'code': code} if code else {}))}
    entries = []
    for path, raw in wheels.items():
        name, release = path.split('/')[-1].split('-')[:2]
        entries.append(dict(name=name, version=release, file=path, sha256=hashlib.sha256(raw).hexdigest()))
    files = dict(wheels, **{'manifest.json': json.dumps(manifest).encode(),
             'requirements.lock': json.dumps({'format_version': 1, 'targets': {'linux-x86_64-cp310': entries}}).encode(),
             'schemas/config.json': b'{"type":"object","additionalProperties":false}',
             'schemas/input.json': b'true', 'schemas/output.json': b'true'})
    return files


def package(files=None, **kwargs):
    files = dict(files if files is not None else package_files(**kwargs))
    files['checksums.json'] = json.dumps({path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()
                                        if path not in ('checksums.json', 'signature.json')}, sort_keys=True).encode()
    return zipped(files)
