# Provider gateway and isolated Claude CLI: 19.2.1–19.2.3

Linux `agent.review` supports the existing `framework: claude_cli` through a
deployment-owned, pinned CLI image and the core provider broker. The standard
agent plugin's prompts, review schema, clarification inbox and durable Operation
receipts are reused. Legacy host execution and the offline JSON worker retain
their existing explicit modes. Windows remains a stub behind the shared
`GatewaySession`/backend contract.

19.2.3 adds explicit Claude environment creation and **local-only** readiness.
The user identified `Ready_To_Release` as containing production credentials and
prohibited sending data. That Cycle is not inspected, run or modified; its auth
and host sign-in are not read/imported. Live account acceptance remains unverified.

## Create and check a Claude environment locally

Execution environments → New → Purpose → **Claude Code review · scoped OAuth
gateway** accepts a reviewed image manifest, executable interpreter/Claude image
targets, exact HTTPS endpoint, reviewed numeric IP pins, full model and secret
reference. The native backend generates RO project access, bounded 128 MiB
scratch, the pinned worker and a v2 route. The form's request budget is one call;
advanced route options remain editable in Document. No existing Service/Cycle
is assigned or migrated. The form contains no token-value field.

Generate and Validate resolve configuration without executing the CLI. Save
writes only the reviewed deployment revision. **Check runtime locally** checks
policy, worker artifact and native prerequisites, using synthetic local probes.
It never opens the Cycle secret store, inspects host sign-in, resolves provider
DNS, sends a provider request or verifies account access. Diagnostics explicitly
show `credential_access: not_read`, `provider_request: not_sent` and
`account_acceptance: not_verified`. A ready result is local readiness only;
the selected CLI version, authentication and live endpoint can still require
separate acceptance. Failed native readiness does not imply a host fallback.

The equivalent CLI accepts an unsaved deployment and exact model in a JSON
request with fields `deployment`, `worker: agent.review`, and `model`:

```sh
qavector-core --runtime-deployment-check --from=readiness-request.json
```

An explicit image helper runs with the reviewed Linux Python interpreter, using
only the selected local native Claude/BusyBox tools, Python stdlib and their
dynamic dependencies. BusyBox is required for existing native prerequisite probes.
The helper never reads sign-in or downloads dependencies; it invokes `ldd` only
for the reviewed executable files. Choose a new persistent image directory;
neither the helper nor the GUI automatically overwrites an existing image.

```sh
PYTHONPATH=. /path/to/reviewed/python examples/execution_runtime/prepare_claude_image.py \
  --claude=/path/to/reviewed/claude --busybox=/path/to/reviewed/busybox \
  --output=/path/to/new-image-directory
```

The output `image.json` is the reviewed manifest to select in GUI;
`PREPARATION.json` contains the image identity and interpreter/CLI targets.
Review the file list and hashes. It does not create a Cycle or save a deployment.
Changes to pinned files require rebuilding/reviewing the image and a new revision.

## Approved route

`RuntimeDeployment.spec.provider_gateways` contains at most eight named routes.
Each route is part of the deployment revision. Changing endpoint, IPs, models,
credential reference or budgets requires a new reviewed revision and explicit
selection in the Cycle. A saved profile never grants workload network access.

| Field | Contract |
|---|---|
| `contract` | `qavector.provider-egress/v2` for the native bridge; v1 remains bounded JSON transport only |
| `provider` | `claude_cli` |
| `endpoint` | Exact HTTPS URL ending in `/v1/messages`, without userinfo/query/fragment |
| `addresses` | 1..8 reviewed canonical numeric IPs; only the first is used, without fallback or runtime DNS |
| `models` | Exact full model names, at most 32; aliases that the CLI expands must not be used |
| `authentication` | v2 requires `claude-oauth-token` |
| `credential` | v2 requires a Cycle secret reference; never a credential value |
| `timeout_seconds` | Default 30, maximum 120; Cycle deadline takes precedence |
| `response_bytes` | Default 65536; maximum 1048576 for v2, 65536 for v1 |
| `request_bytes` | v2 default/maximum 524288 |
| `max_requests` | v2 default 4, maximum 16, for the complete native invocation |
| `allow_private_addresses` | Default false; explicit private/loopback approval for controlled gateways and fixtures |
| `ca_pem` | Optional reviewed PEM CA, at most 16 KiB; otherwise system trust |

Deployment owners review IP pins; address changes require another revision.
Metadata/link-local, multicast, unspecified, reserved and IPv4-mapped addresses
are rejected even with private approval. TLS verifies the endpoint hostname and
CA, with SNI and TLS 1.2 minimum. No proxy environment, DNS, redirects, cookies,
TLS key logs, automatic retries or address fallback are used.

Effective node policy must grant `network.mode: restricted`, the exact route
host/port/tcp, and exactly its credential reference. `none`, `full`, a revoked
endpoint, unapproved model, missing/extra credentials and changed body model fail
closed. Validation is available offline:

```sh
qavector-core --runtime-gateway-validate --from=gateway.json
```

This command validates schema; it does not contact the provider or claim actual
enforcement. GUI Execution environments → Document supports route validation and
Save. Edit node permissions identifies the approved endpoint and credential
delivery. Compact node permission controls use the same worker-specific preflight.
Actual execution records contain route digest, call count and channel state.

## Pinned native image and worker

The approved Linux image must contain the reviewed Claude executable and dynamic
dependencies, the selected Python interpreter/stdlib (including `http.server`,
`socket`, native `_socket`/`select` extensions and their dependencies), and the
unmodified QAVector `claude_review_linux.py` artifact. Every file is hashed by the
existing `RuntimeImage` contract. The CLI is supplied by the deployment owner;
QAVector does not redistribute Claude or discover it through host PATH.

The `spec.workers.agent.review` configuration has this form (paths are native
image targets chosen by the deployment owner):

```json
{
  "contract": "qavector.agent-review/v1",
  "transport": "claude_cli",
  "entrypoint": "/qavector/workers/claude_review_linux.py",
  "argv": ["/approved/python3", "-I", "-B", "/qavector/workers/claude_review_linux.py"],
  "claude": "/approved/claude",
  "gateway": "claude",
  "credentials": {"oauth_token": "CLAUDE_TOKEN"}
}
```

`gateway` references a v2 route in the same deployment; the credential must match
that route. This worker shape excludes arbitrary `provider_argv`. The Cycle
declares the secret, selects the pinned deployment and uses `framework:
claude_cli` with the approved full model name. Node mounts must be read only;
scratch is bounded (at least 8 MiB, with sufficient capacity for the selected CLI).
No host Claude configuration, plugins, hooks, sessions or credentials are mounted.

The worker starts the pinned executable with fixed restricted/safe settings,
empty settings sources and MCP configuration, disabled hooks, Read/Grep/Glob
tools, and no session persistence. Unsupported CLI flags fail closed. Compatibility
was verified with the locally installed Claude Code **2.1.291** against a local TLS
fixture; future CLI versions require another pinned image and acceptance run.
Evidence files and prompts/results have explicit protocol size bounds.

## Scoped browser subscription sign-in

Use the official `claude setup-token` browser flow outside the runtime, then store
the resulting OAuth token explicitly as the declared `CLAUDE_TOKEN` in the
existing Cycle secrets UI/store. Nothing automatically reads or imports host
sign-in. The token is a subscription OAuth credential, not a substituted API key.
See [official authentication documentation](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token).

Only core receives the actual scoped token. It adds the bearer header after
route/policy authorization and certificate verification. The isolated CLI receives
a fresh invocation capability in the OAuth environment variable and points its
`ANTHROPIC_BASE_URL` at a private loopback HTTP bridge. It never receives the
actual token. Raw request/response frames are excluded from logs; literal and
JSON-escaped secret echoes are redacted incrementally across stream boundaries.
This protects against accidental echo, not arbitrary secret encodings.

## Native boundary, cancellation and recovery

Bundled Bubblewrap creates a fresh network namespace with private loopback and
no external interfaces. Seccomp permits IPv4/IPv6 stream sockets there, while
denying Unix, raw, datagram and netlink sockets, namespace escapes and privileged
operations. Offline workers retain their stricter no-socket filter. Services and
sibling workers cannot obtain this channel.

Only framed data travels over private process stdin/stdout pipes to core. The
broker checks exact path, bounded JSON, approved model, sequential identifiers,
headers and request count, then streams bounded uncompressed JSON or SSE back.
Canonical duplicate bodies are rejected. No arbitrary URL/header proxy is exposed.
The CLI's `beta=true` query is normalized locally; upstream routing remains fixed.

Stop/deadline abort the provider socket, wait for the transport thread and reap
the native namespace. Cleanup is unconfirmed if any transport owner remains.
The existing durable review Operation starts before native invocation. A failure
after a possible provider call leaves an uncertain receipt; retries/Resume cannot
silently issue another call. A confirmed failure before any call is recorded as
not delivered. Successful receipts replay the existing result without new traffic.

Native fixture acceptance covers final review/artifacts, credential redaction,
external/host-loopback/Unix/raw socket denial, Stop, timeout, no repeated call, and
the installed CLI's real Messages SSE protocol. Tests never contact a paid endpoint
or perform browser login. Real account/provider acceptance remains a separate step
after an owner supplies an approved image, route/IP pins and scoped OAuth secret.

Bundled Bubblewrap attribution/source/license gates and offline Help → About →
Third-Party Licenses remain mandatory. No system/PATH Bubblewrap fallback is added.
