# QAVplug Starter

Installable authoring example for QAVector 0.16.50 on Linux x86_64/cp310.
One package contains two contributions:

- `example.starter.count`: counts values strictly above a configured threshold.
- `example.starter.echo`: demonstrates AgentBackend describe/execute and echoes
  the supplied task. It performs no AI inference or provider request.

The package requests no workspace, network, process or credential access.
Both contributions execute through the managed worker. The SDK comes from
QAVector's pinned runtime; do not bundle an SDK wheel.

Step-by-step authoring guide in the QAVector repository:
[`docs/qavplug-authoring.md`](../../docs/qavplug-authoring.md).

From this directory, with Python 3.10:

```sh
python3.10 -m venv .venv
.venv/bin/python -m pip install 'setuptools>=61' wheel 'packaging>=23'
.venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist/wheels .
.venv/bin/python tools/build_qavplug.py
```

Output: `dist/example.starter-1.0.0.qavplug`.
Build scripts are host-side author tooling, not part of the plugin wheel.
`tools/prepare_demo.py` creates an isolated test data root/deployment/Cycle
using an explicitly selected frozen core; it never imports the plugin itself.

For your own release, replace the example identities, publisher, supported
QAVector version, license attribution and implementation together. Bump both
manifest and Python package versions when publishing changed content.
