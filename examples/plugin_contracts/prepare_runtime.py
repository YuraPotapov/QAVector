"""Build a relocatable managed Python bundle from a reviewed build interpreter.

Release/build tooling only: no plugin code, downloads or account access. The
installed application uses the resulting descriptor, never host Python/pip.
"""

import argparse
import json
from pathlib import Path
import platform
import shutil
import sys

from packaging.markers import default_environment
from packaging.tags import sys_tags

from examples.execution_runtime.prepare_claude_image import prepare
from execution_runtime.backends.linux import RuntimeImage
from execution_runtime.backends.linux.filesystem import RuntimeFile
from plugin_host.archive import sha256


def build(output, busybox):
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Reuse the reviewed ELF/stdlib/dependency curator, then remove the Claude
    # adapter and stand-in executable: this bundle is provider-neutral.
    prepared = prepare(output / 'curation', busybox, busybox)
    image = json.loads(Path(prepared['image']).read_text())
    payload = output / 'payload'
    payload.mkdir(mode=0o700)
    files = []
    for item in image['files']:
        target = item['target']
        if target == '/approved/claude' or target.startswith(('/qavector/site/qavector_agent_claude/', '/qavector/workers/')):
            continue
        raw = Path(item['source']).read_bytes()
        if sha256(raw) != item['sha256']:
            raise ValueError('Reviewed runtime input changed during preparation')
        local = payload / item['sha256']
        if not local.exists():
            local.write_bytes(raw)
            local.chmod(0o500 if item['executable'] else 0o400)
        files.append(dict(item, source='payload/' + item['sha256']))
    document = {'format_version': 1, 'target': 'linux-' + platform.machine() + '-cp%d%d' % sys.version_info[:2],
        'python_version': platform.python_version(), 'tags': [str(tag) for tag in sys_tags()],
        'markers': default_environment(), 'sdk_version': '1.0.0', 'interpreter': prepared['interpreter'],
        'image': {'identity': RuntimeImage(tuple(RuntimeFile(**dict(item, source=str(output / item['source']))) for item in files)).identity,
                  'files': files}}
    (output / 'plugin-python.json').write_text(json.dumps(document, indent=2) + '\n')
    shutil.rmtree(output / 'curation')
    return output / 'plugin-python.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--busybox', required=True)
    options = parser.parse_args()
    print(build(options.output, options.busybox))


if __name__ == '__main__':
    main()
