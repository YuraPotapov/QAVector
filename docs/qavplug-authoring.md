# Building a `.qavplug` package, step by step

This guide targets **QAVector 0.16.50** and its **Linux x86_64 / CPython 3.10**
managed runtime (`linux-x86_64-cp310`). The package format is universal. The
currently implemented extension points are `node_provider` and `agent_backend`;
a new contribution kind requires a corresponding extension point in core.

You will build an installable package with two contributions:

- `example.starter.count`: an ordinary node that counts numbers above a threshold.
- `example.starter.echo`: an AgentBackend contract example that echoes a task.
  It performs no AI inference, provider request or paid API call.

A package may contain either kind or both. An agent is not required to create
a `.qavplug`.

Complete source: [`examples/qavplug_starter`](../examples/qavplug_starter/).
The plugin implementation lives in `src/` and is distributed as a wheel.
`tools/` contains author-side utilities. QAVector runs installed contributions
in its own managed worker; it does not import them into the GUI or core process.

## 1. Prepare your build tools and copy the starter

You need a QAVector checkout containing the starter, Python 3.10 with `venv`,
and a QAVector 0.16.50 Linux installation for verification. Plugin users do not
need to install Python or pip: the application ships its managed interpreter.

The commands below use Bash. Set the checkout path for your machine:

```bash
export QAVECTOR_SRC=/path/to/your/checkout
export QAVECTOR_PLUGIN_DIR="$HOME/projects/my-qavplug"

python3.10 - <<'PY'
import os
from pathlib import Path
import shutil

source = Path(os.environ['QAVECTOR_SRC']) / 'examples/qavplug_starter'
destination = Path(os.environ['QAVECTOR_PLUGIN_DIR'])
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
    '.venv', '.keys', 'demo', 'dist', 'build', '__pycache__', '*.egg-info'))
print(destination)
PY

cd "$QAVECTOR_PLUGIN_DIR"
python3.10 -m venv .venv
.venv/bin/python -m pip install 'setuptools>=61' wheel 'packaging>=23'
```

The destination must be new; copying refuses to overwrite an existing project.
The pip command installs tools in the author's build environment. QAVector's
plugin installer does not run pip or download dependencies.

For the first pass, keep the starter identities unchanged. Verify the complete
workflow before adapting it to your own package.

## 2. Understand the project layout

```text
my-qavplug/
├── pyproject.toml
├── manifest.json
├── README.md
├── LICENSE
├── src/qavplug_starter/
│   ├── __init__.py          # __version__ = "1.0.0"
│   ├── count.py             # CountProvider
│   └── echo.py              # EchoBackend
├── schemas/
│   ├── count-config.json
│   ├── count-input.json
│   ├── count-output.json
│   └── echo-config.json
└── tools/
    ├── build_qavplug.py      # wheels → lock/checksums → .qavplug
    └── prepare_demo.py       # fresh data root, deployment and test Cycle
```

The packager generates `requirements.lock`, `checksums.json` and the archive.
Do not maintain their hashes by hand.

## 3. Choose identities and a version

These names serve different purposes:

| Field | Starter value | Purpose |
|---|---|---|
| `plugin_id` | `example.starter` | Package identity for import, activation and version management |
| Node contribution ID | `example.starter.count` | The operation selected by a Cycle step |
| Agent contribution ID | `example.starter.echo` | The `backend_ref` in an Agent Profile |
| `runtime.distribution` | `qavplug-starter` | Python distribution name in wheel METADATA |
| Python package | `qavplug_starter` | Importable package inside the worker |
| `entrypoint` | `qavplug_starter.count:CountProvider` | Python module and implementation class |
| Role ID | `agent.demo` | A profile name within a particular Cycle |

When adapting the starter, update the manifest, Python metadata, result
identities and Cycle references together. External packages cannot claim the
reserved `qavector.*` namespace.

Use a stable `major.minor.patch` version, such as `1.0.0`. The starter defines
`__version__` in `src/qavplug_starter/__init__.py`. Its `pyproject.toml` reads
that value, and both implementations return it in their results. Update
`manifest.json` as well. The packager checks that the own-wheel version matches
the manifest version.

## 4. Implement an ordinary node

[`count.py`](../examples/qavplug_starter/src/qavplug_starter/count.py) contains
the complete implementation:

```python
from . import __version__

class CountProvider:
    async def execute(self, request, context, emit):
        count = sum(value > request['config']['threshold']
                    for value in request['inputs']['values'])
        return {
            'schema_version': '1.0.0',
            'attempt_id': request['attempt_id'],
            'contribution_ref': 'example.starter.count',
            'plugin_version': __version__,
            'status': 'completed',
            'summary': 'Counted %d values above the threshold.' % count,
            'output': {'count': count},
            'artifact_refs': [],
            'error': None,
        }
```

Contract rules:

1. `execute` is async and accepts `request`, `context` and `emit`.
2. The class is constructed without arguments. Module imports and constructors
   should not initiate model work or external actions.
3. Copy the current request's `attempt_id`; do not generate a replacement.
4. `contribution_ref` must match the manifest contribution ID, and
   `plugin_version` must match the selected package version. `schema_version`
   is the public contract version, independent of your package version.
5. A successful result uses `status: completed`, `error: null`, and an `output`
   matching the declared output schema.
6. Other terminal statuses require the corresponding normalized error contract.
   See [Plugin contracts](plugin-contracts-v1.md) and the SDK schemas. Returning
   only `{'error': 'some text'}` is not a valid result.

`context` carries opaque scoped-service handles. A handle is not a filesystem
path or raw credential. Short operations do not need to emit progress events.
If you use `emit`, follow the node event schema; terminal events belong to the SDK.

## 5. Define config, input and output schemas

The starter uses self-contained Draft 2020-12 JSON Schemas:

| Schema | Example value |
|---|---|
| `count-config.json` | `{"threshold": 2}` |
| `count-input.json` | `{"values": [1, 3, 5]}` |
| `count-output.json` | `{"count": 2}` |

For example, the config schema is:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {"threshold": {"type": "number"}},
  "required": ["threshold"],
  "additionalProperties": false
}
```

The installer rejects remote and cross-file `$ref` values. Reuse definitions
through `$defs` and document-local references such as `#/$defs/item` within
the same document. A schema does not create files or grant permissions.

## 6. Declare contributions, compatibility and permissions

The full [`manifest.json`](../examples/qavplug_starter/manifest.json) declares
both contributions. The ordinary node entry is:

```json
{
  "kind": "node_provider",
  "id": "example.starter.count",
  "entrypoint": "qavplug_starter.count:CountProvider",
  "config_schema": "schemas/count-config.json",
  "input_schema": "schemas/count-input.json",
  "output_schema": "schemas/count-output.json"
}
```

Review these package-level fields:

- `format_version: 1`: the manifest format version.
- `plugin_id`, `name`, `version`, `publisher`, `description`, `license`:
  identity and display metadata. `publisher` is display text; signatures
  establish a verified publisher identity separately.
- `compatibility.qavector`: supported application releases. The starter uses
  `==0.16.50`. Test another release before changing this declared support range.
- `plugin_api`, `host_api`: public API ranges. `agent_api` is additionally
  required for agent backend contributions.
- `runtime.distribution`: the own-wheel distribution name; `runtime.python`:
  supported worker Python versions; `targets`: supported managed runtime/ABIs.
- `permissions_requested`: requested grants; `external_requirements`: external
  CLI prerequisites; `dependencies`: the JSON wheel lock and plugin dependencies.
  This example has no external prerequisites or plugin dependencies.

QAVector/API ranges support `==`, `>=`, `>`, `<=`, `<` and comma-separated
intersections, for example `>=0.16.50,<0.17.0`. This is your tested support
declaration, not an automatic guarantee about future patches. Carets, tildes,
wildcards, `!=` and prereleases are unsupported. Python dependency requirements
use Python specifiers separately.

For a **node-only package**, remove the echo contribution and
`compatibility.agent_api`; you can also remove `echo.py`. The demo helper assumes
the unchanged two-contribution starter, so adapt its generated Cycle when you
change the set of contributions.

## 7. Implement an AgentBackend when needed

An agent backend additionally implements:

```python
async def describe(self, profile, context):
    # Return AgentCapabilities for this profile/model/runtime.
    ...

async def execute(self, request, context, emit):
    # Return an AgentResult for the current attempt.
    ...
```

The complete minimal example is
[`echo.py`](../examples/qavplug_starter/src/qavplug_starter/echo.py).
`describe` reads `profile.backend_options.prefix` and declares capabilities.
`execute` returns `{"answer": "<prefix><task>"}`. Its contribution's config schema
validates `profile.backend_options`, rather than an ordinary node's `with.config`.

Compared with an ordinary node result, `AgentResult`:

- uses `backend_ref` instead of `contribution_ref`;
- includes `session_ref` and `usage`, set to `null` when unavailable;
- must not invent token usage, cost or a provider session.

Advertise only implemented capabilities. The example has no streaming, tool
events, interactive input or session resume. Its managed host supplies the hard
wall-time boundary and confirms process cleanup. Profile IDs such as
`agent.developer`, `agent.tester` and `agent.business.analyst` are Cycle roles;
several roles can select the same backend contribution.

A real AI backend needs a provider implementation through approved Host API
services. Packaging an AgentBackend does not call a model automatically.

## 8. Build the Python wheel

From the root of your copied starter:

```bash
.venv/bin/python -m pip wheel \
  --no-deps --no-build-isolation --wheel-dir dist/wheels .
```

Expected output:

```text
dist/wheels/qavplug_starter-1.0.0-py3-none-any.whl
```

`--no-deps` is intentional. QAVector supplies its pinned SDK and forbids
bundling an SDK wheel. The starter declares `qavector-sdk>=1.0.0,<2.0.0` in
Python metadata; the installer satisfies it from the runtime. You do not need
to find or install a separate public SDK pip distribution to build this example.

For other Python dependencies:

1. Declare them in `project.dependencies`.
2. Prepare wheels for their complete transitive dependency closure in the wheelhouse.
3. Select wheels for the managed runtime's Python, ABI and platform, rather than
   whichever interpreter happens to build the package. Native dependencies need
   compatible binary wheels.
4. Keep one selected version of each distribution in the wheelhouse.
5. Include the required third-party license materials under `licenses/`.

The installer does not support sdists, dependency URLs/extras, `.pth`, startup
hooks or generated console-script wrappers. Do not package your virtualenv or
an entire `site-packages`. This starter packager handles a single target;
multiple ABI targets require separately selected wheel sets in the target lock.

## 9. Assemble the `.qavplug`

```bash
.venv/bin/python tools/build_qavplug.py
```

Output:

```text
dist/example.starter-1.0.0.qavplug
```

The utility prints the archive path, its SHA-256 and `signed: false`. It does
not import the plugin classes or execute their code. The archive is a ZIP with
these entries **at its root**:

```text
manifest.json
requirements.lock
checksums.json
README.md
LICENSE
schemas/*.json
wheels/qavplug_starter-1.0.0-py3-none-any.whl
signature.json             # only when explicitly signing the archive
```

Despite the name, `requirements.lock` is a JSON document:

```json
{
  "format_version": 1,
  "targets": {
    "linux-x86_64-cp310": [
      {
        "name": "qavplug-starter",
        "version": "1.0.0",
        "file": "wheels/qavplug_starter-1.0.0-py3-none-any.whl",
        "sha256": "<SHA-256 of the wheel bytes>"
      }
    ]
  }
}
```

The script calculates the real hash. `checksums.json` maps every payload file
to its SHA-256, excluding itself and `signature.json`. These file hashes differ
from the digest of the complete `.qavplug`. Repacking the same content with
different archive bytes can change the package digest. Do not put an extra
outer `my-qavplug/` directory inside the ZIP.

## 10. Prepare an isolated verification environment

Use a fresh data root for the first test. This keeps the verification package,
deployment and run records separate from your regular QAVector profile.

```bash
export QAVECTOR_CORE=/opt/qavector/core/qavector-core
"$QAVECTOR_CORE" --version

.venv/bin/python tools/prepare_demo.py \
  --core "$QAVECTOR_CORE" --output "$PWD/demo"

export QAVECTOR_DEMO_HOME="$PWD/demo/data"
```

`--core` must point to the actual **frozen executable**, rather than the
`/usr/bin/qavector` wrapper or a source `session_launcher.py`. For a local build,
use `/absolute/path/to/qavector/build/dist/core/qavector-core`.

The helper verifies the shipped `plugin-python.json` payload hashes, creates
a new `demo/` directory, and saves an offline deployment through the selected
core's CLI. The deployment pins that exact managed image. It does not select
a Python interpreter from PATH.

The helper also creates `demo/cycles/qavplug_starter_demo.yaml`, with the exact
`execution.runtime` revision, count step and `agent.demo` profile. It refuses to
overwrite an existing `demo/`; choose a new output name for another run, and
adjust `QAVECTOR_DEMO_HOME` and subsequent paths accordingly.

Preparing a deployment does not install or activate the plugin. A plugin must
not provision its own deployment, runtime image or permission grants; these
are host-owned settings.

## 11. Inspect the archive with QAVector

```bash
CMS_HOME="$QAVECTOR_DEMO_HOME" "$QAVECTOR_CORE" --plugins inspect \
  dist/example.starter-1.0.0.qavplug > demo/inspection.json

.venv/bin/python -m json.tool demo/inspection.json
```

Check:

- `ok: true` and `result.compatibility.compatible: true`;
- `result.runtime_ready: true`, meaning a managed runtime descriptor is available;
- the expected IDs, version, target, contributions and digest;
- no workspace/network access and empty process/credential grants;
- `result.trust.publisher_identity: local:unsigned` for this unsigned example.

`runtime_ready` is not a kernel/sandbox readiness guarantee. Import performs
the actual isolated healthcheck after the package has been approved.

## 12. Approve, import and activate

After reviewing the inspection, save its decision template for this exact archive:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path

inspection = json.loads(Path('demo/inspection.json').read_text())
assert inspection['ok'] and inspection['result']['runtime_ready']
approval = inspection['result']['approval_required']
assert approval is not None
Path('demo/approval.json').write_text(json.dumps(approval, indent=2) + '\n')
PY

CMS_HOME="$QAVECTOR_DEMO_HOME" "$QAVECTOR_CORE" --plugins import \
  dist/example.starter-1.0.0.qavplug --approval demo/approval.json

CMS_HOME="$QAVECTOR_DEMO_HOME" "$QAVECTOR_CORE" --plugins list
CMS_HOME="$QAVECTOR_DEMO_HOME" "$QAVECTOR_CORE" --plugins activate example.starter 1.0.0
```

Approval binds the exact digest, publisher identity and complete requested grants.
Import runs the offline isolated import healthcheck and initially stores the
package as `installed_inactive`. Activation selects its version for new runs.
Both contributions in this package use that selected package version.

The import healthcheck is not execution of the plugin's business operation or
a behavioral safety audit. An unsigned archive can be locally approved, but
the display `publisher` field does not authenticate an author. See step 16 for
publisher signatures.

## 13. Run the verification Cycle

```bash
CMS_HOME="$QAVECTOR_DEMO_HOME" "$QAVECTOR_CORE" \
  --cycles-dir="$PWD/demo/cycles" --cycle-run=qavplug_starter_demo \
  --cycle-jobs=2 --events=- > demo/events.jsonl
```

The steps are independent. Expected `cycle.step.end` results:

| Step | `outputs.output` |
|---|---|
| `count` | `{"count": 2}` |
| `echo` | `{"answer": "Demo: Hello QAVector"}` |

Print the results:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path

for line in Path('demo/events.jsonl').read_text().splitlines():
    event = json.loads(line)
    if event.get('kind') == 'cycle.step.end':
        print(event.get('step'), event.get('status'), event.get('outputs', {}).get('output'))
PY
```

Run records and exact package/runtime pins remain in the demo data root. The
helper writes JSON into the `.yaml` file; JSON is valid YAML and uses the normal
QAVector Cycle loader.

An ordinary contribution is selected like this in your own YAML:

```yaml
steps:
  - id: count
    plugin: example.starter.count
    with:
      plugin_version: '==1.0.0'
      config:
        threshold: 2
      inputs:
        values: [1, 3, 5]
```

An agent role uses a profile:

```yaml
agent_profiles:
  agent.demo:
    backend_ref: example.starter.echo
    backend_version: '==1.0.0'
    limits:
      wall_time_seconds: 30
    backend_options:
      prefix: 'Demo: '
steps:
  - id: echo
    plugin: agent
    with:
      agent_profile: agent.demo
      task: Hello QAVector
```

Both fragments require an `execution.runtime` section in the Cycle root, with
the deployment name, approved revision and project directory. The generated
demo has the full working declaration. Do not invent a revision or combine
this path with legacy `restrictions`.

External AgentBackends use the generic `agent` node and a profile. Compatibility
nodes such as `agent.review` and `agent.edit` do not select an external backend.

## 14. Import into your regular GUI profile

After verifying the isolated demo, in your regular QAVector application:

1. Open **Plugins → Import plugin…**.
2. Select `dist/example.starter-1.0.0.qavplug`.
3. Review the publisher, compatibility, digest and permissions, then approve import.
4. Select the imported package and click **Activate version**.
5. Use `example.starter.count` in a Cycle, or select `example.starter.echo` as
   an agent profile's backend. Configure an approved managed execution runtime
   for that Cycle's installed contributions.

Your regular application uses a different store from the demo. Its deployment
and Cycle settings must be prepared and reviewed in the intended data root;
importing the archive alone does not transfer the demo's runtime setup to the
current GUI project. See the
[Cycle runtime boundary](cycle-managed-plugins.md#runtime-and-permission-boundary).

## 15. Add permissions and integrations when required

The starter requests:

```json
{"workspace": "none", "process": [], "network": "none", "credentials": []}
```

Installed workers are offline, cannot arbitrarily fork/create threads, and
do not receive raw secret files. Importing a Python module grants no additional
host access. For approved HTTPS or command services, use the scoped Host API.
For example, inside the managed worker:

```python
from qavector_sdk.worker import host_call

response = await host_call(context['tools_handle'], 'network.request', {
    'route': 'api',
    'body': {'model': 'approved-model', 'input': request['task']},
})
```

This requires a different permission configuration from the offline starter:

1. Declare the requested permissions in the manifest and obtain fresh consent.
2. Configure reviewed `spec.plugin_services.<contribution_id>` in the deployment.
3. Ensure the effective policy allows the endpoint/tool/secret.
4. Supply the exact route, numeric address pins, credential aliases and approved
   executable hashes according to [Plugin services v1](plugin-services-v1.md).

`network: policy_controlled` does not enable direct `requests.get`, DNS or sockets
inside the worker. The host adds authorization to an approved request without
returning the raw credential. Approved commands run in separate supervised
Runtime jobs. Workspace permission narrows existing deployment grants; it does
not create a mount or permission by itself.

Do not print to stdout: the SDK uses it for JSON-RPC. Use stderr for diagnostics
and schema-valid `emit` calls for execution events. Never log credentials.
Avoid `subprocess`, threads and `asyncio.to_thread`; use Host API command services.
Protocol frames are limited to 256 KiB, events to 4096 per attempt, and pending
callbacks to 16. Interactive sessions and arbitrary artifact publication are
not supported by this transport.

## 16. Optionally sign the package with Ed25519

Sign **before the first import of a release**. Adding a signature changes archive
bytes, digest and publisher identity. Use separate fresh demo stores when
testing unsigned and signed variants of the same example ID/version. Keep the
private key outside the wheel, archive and repository.

```bash
.venv/bin/python -m pip install cryptography
.venv/bin/python - <<'PY'
import base64
import os
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

directory = Path('.keys')
directory.mkdir(mode=0o700, exist_ok=True)
key = Ed25519PrivateKey.generate()
private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
fd = os.open(directory / 'publisher.pem', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as stream:
    stream.write(private)
public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
(directory / 'publisher-public.base64').write_text(base64.b64encode(public).decode() + '\n')
PY

.venv/bin/python tools/build_qavplug.py \
  --sign-key .keys/publisher.pem --key-id starter-publisher \
  --output dist/example.starter-1.0.0-signed.qavplug
```

Create a separate signed-package demo and explicitly enroll the **public** key
there before inspecting the signed archive:

```bash
.venv/bin/python tools/prepare_demo.py \
  --core "$QAVECTOR_CORE" --output "$PWD/demo-signed"
export QAVECTOR_SIGNED_HOME="$PWD/demo-signed/data"

CMS_HOME="$QAVECTOR_SIGNED_HOME" "$QAVECTOR_CORE" --plugins trust-key \
  starter-publisher "$(cat .keys/publisher-public.base64)"

CMS_HOME="$QAVECTOR_SIGNED_HOME" "$QAVECTOR_CORE" --plugins inspect \
  dist/example.starter-1.0.0-signed.qavplug > demo-signed/inspection.json
.venv/bin/python -m json.tool demo-signed/inspection.json
```

After reviewing the signed inspection, save its `result.approval_required` to
`demo-signed/approval.json`, using the Python snippet from step 12 with both
paths changed to `demo-signed/`. Then import and activate it:

```bash
CMS_HOME="$QAVECTOR_SIGNED_HOME" "$QAVECTOR_CORE" --plugins import \
  dist/example.starter-1.0.0-signed.qavplug --approval demo-signed/approval.json
CMS_HOME="$QAVECTOR_SIGNED_HOME" "$QAVECTOR_CORE" --plugins activate example.starter 1.0.0
CMS_HOME="$QAVECTOR_SIGNED_HOME" "$QAVECTOR_CORE" \
  --cycles-dir="$PWD/demo-signed/cycles" --cycle-run=qavplug_starter_demo --events=-
```

The GUI key-enrollment equivalent is **Plugins → Manage → Trust a publisher key…**.
Establish the association between the public key and publisher through your
trusted distribution channel before enrolling it.

`signature.json` contains `algorithm`, `key_id` and a base64 `signature`.
The signed bytes are exactly:

```python
b'QAVPLUG-SIGNATURE-V1\n' + checksums_bytes
```

Do not reformat `checksums.json` after signing. A signature authenticates the
verified payload's origin; code behavior and granted permissions still matter.

## 17. Release the next version

For `1.0.1`, update `__version__` and `manifest.version`, then use a clean
wheelhouse for the new release:

```bash
.venv/bin/python -m pip wheel --no-deps --no-build-isolation \
  --wheel-dir dist/wheels-1.0.1 .
.venv/bin/python tools/build_qavplug.py --wheelhouse dist/wheels-1.0.1
```

Repeat inspection, consent, import, tests and activation. New versions do not
inherit consent automatically. A different archive registered with the same
ID/version is rejected; never reuse a published version for a different payload.

Activation/rollback chooses a version for new runs. Existing runs and Resume
retain their exact package/digest/runtime pins. Retained run references can
block removal, and rollback does not undo external effects of earlier work.
After upgrading QAVector, recheck compatibility:

```bash
CMS_HOME="$QAVECTOR_DEMO_HOME" "$QAVECTOR_CORE" --plugins check
CMS_HOME="$QAVECTOR_DEMO_HOME" "$QAVECTOR_CORE" --plugins diagnostics
```

## 18. Troubleshoot common failures

| Symptom | Check |
|---|---|
| `PACKAGE_INVALID` | ZIP root, checksums, schema paths, wheel RECORD and supported layout |
| `PLUGIN_RUNTIME_INCOMPATIBLE` | Shipped descriptor, Python/target/ABI, Linux isolation readiness and exact SDK image |
| `QAVECTOR_VERSION_INCOMPATIBLE` / `PLUGIN_API_INCOMPATIBLE` | Declared application/API ranges and the actual host versions |
| `PLUGIN_HEALTHCHECK_FAILED` | Entrypoint module in the wheel, complete dependencies, no external work during import |
| `PLUGIN_NOT_AVAILABLE` | Correct data root, installed/active version and contribution ID |
| `PACKAGE_VERSION_CONFLICT` | Different content or publisher already registered for the same ID/version |
| `OUTPUT_SCHEMA_INVALID` | Output matches both the static schema and any additional request schema |
| `CAPABILITY_UNSUPPORTED` | Profile requirements match what `describe` actually supports |
| `AGENT_PREREQUISITE_MISSING` | External executable pinned in the managed image and exact prerequisite bindings supplied |
| Worker protocol failure after `print` | Diagnostics go to stderr; stdout contains SDK protocol only |
| Imported node cannot run in a Cycle | Activation, `execution.runtime`, approved project binding and revision |
| Resume fails after core upgrade | Original exact runtime/SDK/package remains available; newer code does not replace old pins |

Check CLI JSON fields `ok`, `code` and `message`, and the process exit code.
Compatibility inspection may instead report detailed issues in `result.compatibility`.
Do not edit installed payloads in place; build and verify a new release.

## References

- [Archive, wheel, trust and installer contract](qavplug-installer-v1.md).
- [Public contracts and JSON Schemas](plugin-contracts-v1.md).
- [Installed plugins in Cycle](cycle-managed-plugins.md).
- [Scoped HTTPS, credentials and commands](plugin-services-v1.md).
- [Worker and Host API implementation](plugin-host-runtime-v1.md).
- [Exact version pins and Resume](cycle-plugin-pins.md).

This guide covers the currently implemented public extension points. Arbitrary
GUI extensions, new contribution kinds and Windows worker execution require
their own core implementation before packages can use them.
