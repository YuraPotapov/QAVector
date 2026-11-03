# Plugin core completion plan

Work in numbered increments. No commits requested; changes remain in the working tree.

1. **Role profiles in Cycle — implemented for the bundled Claude compatibility adapter.**
   `agent_profiles` declarations, the universal `agent` node, role selection in
   the node editor, validation, output contracts and Resume fingerprints.
   Existing nodes/YAML retain their existing behavior. See
   [Agent profiles in Cycle](cycle-agent-profiles.md).
2. **Registry and exact run bindings — implemented for bundled Claude.**
   New profile and legacy Claude runs resolve through the persistent registry.
   Version/content pins are saved before execution, included in checkpoints
   and rechecked on Resume and before each backend call. Old pre-pin runs keep
   their historical recovery behavior. See [Cycle package pins](cycle-plugin-pins.md).
3. **Claude provider gateway integration — implemented for Linux review.**
   The pinned isolated worker calls the shared AgentBackend and returns AgentResult,
   retaining its network, credentials and CLI restrictions. Backend/SDK resources
   are checked against the core build. Existing images need preparation and
   deployment reapproval. See [gateway integration](claude-gateway-backend-v1.md).
4. **Universal `.qavplug` installation — implemented through API/CLI on Linux.**
   Bounded archive/wheel validation, Ed25519 trust, digest-bound grants, offline
   managed Python environments and isolated import healthchecks. Atomic version
   activation/rollback, recovery and retention/removal guards support ordinary
   node providers and agent backends. See [installer v1](qavplug-installer-v1.md).
   GUI/Cycle wiring is covered by item 5; clean-machine release acceptance remains.
5. **GUI/Cycle integration — implemented for managed Linux plugins.**
   Plugins manager, contribution discovery, JSON configuration, role selection,
   exact run pins and isolated execution of both contribution kinds. Native
   acceptance covers import, execution, update/disable/Resume, quarantine,
   invalid inputs and cancellation. See [managed plugins in Cycle](cycle-managed-plugins.md)
   for the supported transport and outstanding release/UI capabilities.

The list describes remaining implementation, not a claim of release readiness.
Worker/process/native tests from earlier increments remain part of validation.

## Verification of items 1–2

- Cycle/SDK/Runtime regression suite: 2736 passed, 85 skipped.
- GUI node editor, draft and Cycle page suites: 452 passed.
- Native Bubblewrap stdio, ordinary provider and Claude backend checks: 3 passed.
- Focused profile/pin and registry concurrency checks also passed, including
  concurrent first-run catalog activation and save/reopen of profile definitions.
- Wheel imports and the bundled content digest were checked against the checkout.

Tests use local deterministic CLI fixtures, not live model calls or user credentials.

## Verification of item 3

- Cycle/profile/pin, SDK/plugin and Runtime regression suite: 685 passed,
  76 skipped (native/optional cases were run separately where configured).
- Gateway suite with the project Bubblewrap bundle: 26 passed, 3 optional
  installed-CLI/frozen-build cases skipped.
- Additional native checks passed for normalized usage and paid-answer replay;
  both domain-review and SDK-output-schema failures were replayed without a
  second provider call on Resume.
- Final focused gateway contract checks: 14 passed.
- Offline image preparation and wheel imports passed; backend/SDK/bootstrap
  hashes match the checkout and prepared image.

The system Bubblewrap lacks the required bounded-tmpfs option. Native gateway
acceptance used the existing project bundle with `--size` support; no isolation
requirements were relaxed. All provider calls in these tests use local fixtures.

## Verification of item 4

- Final plugin/SDK, Claude, profile/pin and packaging regression set: 312 passed,
  4 native tests skipped in that run.
- Those 4 native installer tests passed separately with the project Bubblewrap
  bundle: both contribution kinds, host/network/write restrictions, hard startup
  limits, failed-update preservation and relocated managed Python/SDK import.
- Installed wheel imports and JSON CLI smoke check passed. The managed Python
  bundle was prepared offline and tested after relocation.
- Full frozen/deb and clean-machine release acceptance remain outstanding;
  GUI/Cycle integration is the next numbered work item.

## QAVector/package version compatibility

Each installable manifest now declares `compatibility.qavector` separately from
public API and Python/ABI constraints. Import, activation, resolution, managed
handoff and Resume check the running release. `--plugins check`, `list` and
`inspect` expose current versions, requirements and concrete incompatibilities.
Installed content, active pointers, disabled states and run locks are preserved
across core upgrades; no compatible version is substituted automatically.
Legacy undeclared ranges remain inspectable but do not authorize execution.

Validation: 606 regression tests passed (4 native tests skipped in that run),
5 targeted native installer/gateway checks passed, and the final focused
compatibility/contracts/registry/installer/worker set passed 253 tests, including
the public RPC error code. The bundled Claude metadata version is now 1.0.2;
new image/source pins must be prepared when the bundled inventory changes.
See [the compatibility contract](qavplug-installer-v1.md#qavector-release-compatibility).

## Verification of item 5

- SDK/host/installer/catalog and Cycle regression set: 358 passed.
- GUI Plugins, main window, Cycle page and core bridge: 534 passed; node editor
  regression: 82 passed. After moving Plugins to the bottom QAVector group,
  focused navigation/JSON/role checks: 27 passed. The puzzle icon and placement
  were also rendered and inspected.
- Four native package/Cycle tests passed for both contribution kinds, exact
  update/disable/Resume, quarantine, invalid config, cancellation and timeout.
- Two additional native checks passed after the Runtime wiring update: scoped
  read-only workspace/denied write and network, and cancellation after startup.
- Frozen core import → activation → catalog → Cycle passed with an isolated
  data root and Python/pip absent from PATH. The core shipped its own managed
  Python and native launcher. This is not a complete clean-machine/deb matrix.
- Final bundled-activation/catalog/profile/pin/installer set: 74 passed.
- Unified catalog checks: 6 passed; Plugins page checks: 5 passed. A fresh store
  displays 28 built-in nodes and the bundled Claude backend without registering
  synthetic packages or changing active selections.

No commits were created. Scoped network/credential/command support is described
in [Plugin services v1](plugin-services-v1.md). Interactive/remote sessions,
artifact publication and richer schema-generated forms remain explicit
follow-up work, not capabilities implied by installation.

## Follow-up 1: scoped network, credentials and commands

Implemented for installed node providers and agent backends on Linux:
contribution-scoped deployment configuration, pinned HTTPS routes, credential
aliases, approved executable commands through supervised Runtime jobs, bounded
Host API calls, cancellation and fail-closed uncertain outcomes. Received HTTP
responses followed by validation failure are replayed without sending a second
request. The worker remains offline and cannot fork or read raw credential files.
See [configuration, SDK calls and limits](plugin-services-v1.md).

The Plugins visual review and clean-machine release packaging remain separate
work. In particular, the SDK callback deadline change
requires regenerating the managed Python bundle and approving its new image pin.

Validation (2026-11-02): 402 regression tests passed, covering SDK/Host API,
installer, deployment policy, Cycle bindings/pause and the shared Claude adapter.
Eight native service scenarios passed: scoped HTTPS/credential/tool execution,
received-response replay, unresolved-response Resume rejection, prompt Stop,
tool timeout, unconfirmed cleanup, external AgentBackend credential/model scope,
and a real 31-second command exceeding the previous callback limit. Two existing
native offline scenarios also passed: update/disable/exact Resume and read-only
workspace enforcement. Tests used synthetic secrets and local fixtures only.

## Follow-up 2: native plugin Pause/Continue

Implemented for controlled Linux Cycles running installed plugins and their
scoped command jobs. Runtime freezes pinned namespace trees, confirms stopped
states, continues the same processes and excludes paused time from its active
deadlines. Plugin Host shares the attempt's pause gate and clock; SDK callbacks
rely on the supervised deadline rather than a worker-local wall-clock timer.
The graph/panels use the existing `held_steps` representation. Stop cleans up
both frozen processes and prepared-but-not-started workers.

Already-initiated HTTPS calls retain a finite transport timeout; their response
waits for Continue. An incomplete call or Stop after send remains unknown and
blocks automatic Resume. Remote servers and third-party internal clocks are not
frozen. Other adapters, including the separate Claude provider bridge, are not
automatically opted into this generic plugin pause path.

Validation (2026-11-03): 277 regression tests passed, plus 10 native pause
scenarios and 5 existing native lifecycle scenarios. Native checks cover
descendants, pause longer than timeout, same-attempt Continue, Stop, delayed
worker launch, received/incomplete HTTPS and cancellation during paused HTTP.
The new SDK content requires a rebuilt managed bundle for release. Visual review
of Plugins and clean-machine packaging remain the next two work items.

## Follow-up 3: Plugins visual review

Completed for the searchable catalog in light/dark themes, including live theme
switching. Reviewed wide, 850×650 and 680×750 layouts, empty search, long metadata,
and active/inactive/disabled/incompatible/quarantined states. Category icons,
keyboard focus, source labels and version selection replace the former table.
Details scroll independently of pinned actions and stack beneath the catalog
on narrow windows. Compatibility/quarantine explanations appear near the status;
activation/removal controls reflect lifecycle restrictions. Import review keeps
consent/actions visible and requires compatible approval plus a ready runtime.

Validation (2026-11-03): 108 Plugins/theme/widget tests and 4 main-window
navigation/sidebar tests passed. The final contrast adjustment also passed the
19 focused Plugins checks. Renders were inspected; user plugins and runtime
stores were not modified. No commits were created.

Remaining in this completion stage: rebuild managed runtime/core/GUI and perform
clean-machine install/update acceptance (follow-up 4).

## Follow-up 4: Linux release build and install/update acceptance

Completed on 2026-11-03 for Linux amd64, version **0.16.50**. Rebuilt the managed
Python/SDK image, native launcher, core and GUI together. The DEB and SHA256SUMS
are in `installers/linux/0.16.50/`. No commits or host application installation
were performed. The previous 0.16.49 directory contained only an incomplete
archive, so the genuine, checksum-verified 0.16.48 DEB was used for upgrade.

Fixed the managed payload distribution modes: private build inputs become
0644/0755 public, root-owned package files. Otherwise an ordinary installed user
could not read them. Exact content hashes remain unchanged by that mode fix.
Preview/CI builds now regenerate managed Python too, pin plugin source/schema
changes and require the frozen plugin and installed Plugins checks.

Validation: **139 targeted tests passed** (62 packaging/attribution guards,
56 compatibility/catalog/data-path checks, 20 Plugins GUI checks and one real
frozen import → activation → Cycle execution without system Python/tools in
PATH). Both offline clean-container targets passed actual dpkg installation,
0.16.48 → 0.16.50 upgrade, remove, purge and fresh installation:

- Ubuntu 22.04; evidence: `build/plugins-release-0.16.50-ubuntu/`.
- Debian 12; evidence: `build/plugins-release-0.16.50-debian/`.

Each target verified managed payload hashes/readability as UID 1000, native
bundle integrity, offline notices, desktop library resolution and offscreen GUI
startup on Plugins after upgrade and fresh installation. Both catalogs contained
28 built-in nodes, one bundled Claude backend and no imported packages. Synthetic
configuration, registry and run-pin sentinel bytes/modes/ownership survived every
lifecycle operation. Both owned containers were removed successfully. Cached
dependencies came from the previously verified signed OS repository preparation;
their hashes were rechecked. No host HOME, real credentials or provider calls
were involved.

This closes the four follow-up items for the current Linux stage. It is not a
Windows release, a hosted CI run or a long-running plugin-state migration test.
The registry/run-pin lifecycle fixtures are inert preservation sentinels; runtime
compatibility/pinning is tested separately. Existing exact runtime pins are not
silently migrated to the rebuilt image. Interactive/remote sessions, artifact
publication, richer generated forms and the separate Claude bridge pause path
remain outside this stage.
