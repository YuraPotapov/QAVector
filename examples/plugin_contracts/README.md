# Plugin contract examples

These JSON documents exercise the development-preview SDK described in
[`docs/plugin-contracts-v1.md`](../../docs/plugin-contracts-v1.md).

`node-manifest.json` and `count_provider.py` demonstrate an ordinary executable
node with config/input/output schemas under `schemas/`. It counts values above
a threshold and does not use an agent, model, prompt or Agent Profile.

`qavplug-manifest.json` demonstrates an agent backend contribution. A package
may include both types of contribution. The other JSON files are validated
wire examples, including explicit null usage and scoped context handles.

These are source and metadata fixtures, not installable archives. Package
building, wheels, offline locks, trust, signatures and isolated worker execution
are provided by the separate [installable starter](../qavplug_starter/README.md)
and [authoring guide](../../docs/qavplug-authoring.md). The test here invokes only this repository-owned
fixture directly; an imported plugin must execute in its worker via Runtime.

```sh
python -m pytest tests/test_plugin_contracts.py -q
```
