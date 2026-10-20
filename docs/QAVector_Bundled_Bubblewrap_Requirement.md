# QAVector — Bundled Bubblewrap Runtime Requirement

## 1. Requirement ID

**ER-BB-001 — Self-Contained Bubblewrap Distribution**

## 2. Objective

QAVector must distribute and operate its Linux isolation backend without requiring users to install the system `bubblewrap` package separately.

Bubblewrap must be built from a pinned, reviewed upstream source revision and shipped as part of the QAVector application distribution. The result must integrate with the existing **Execution Runtime**, **Environments**, and **Cycle Node Isolation** architecture.

**Scope:** every executable Cycle node (including `agent`, `git`, `command.shell`, `script`, `test`, `plugin` and future node types), as well as QAVector-managed services such as Odoo, PostgreSQL, Flask and Python processes.

## 3. Functional Requirements

### ER-BB-001.1 — Bundled Binary

- Build Bubblewrap as a separate executable during QAVector's packaging/release process.
- Include it in the distributed application under an application-owned runtime path, for example `runtime/bin/bwrap`.
- Resolve the bundled executable through an application resource/path resolver; do not rely on `/usr/bin/bwrap` or the user's `PATH`.
- Ship all additionally required runtime libraries when legally and technically appropriate, or document unavoidable baseline system ABI requirements.
- Do not require users to execute `apt install bubblewrap`, install Docker, or manually prepare Bubblewrap.
- Keep Bubblewrap as a separate child-process executable rather than linking its implementation into the PySide6 GUI process.

### ER-BB-001.2 — Reproducible Build and Packaging

- Pin the upstream Bubblewrap release/commit; record the version and source checksum.
- Automate the build and packaging steps in CI.
- Produce per-target Linux builds with documented architecture, glibc and kernel compatibility boundaries.
- Evaluate static and bundled-dynamic linking based on verified upstream build capabilities, portability, security updates and licenses; do not assume a fully static binary is always achievable.
- Include relevant license notices, attribution and software-component inventory (SBOM).
- Keep an explicit mechanism for upgrading Bubblewrap when security fixes become available.

### ER-BB-001.3 — Runtime Capability Detection

Before executing a workload that requires isolation, validate:

- the bundled binary exists, is executable and matches the expected version/integrity metadata;
- Linux user, mount, PID and network namespace availability, as required by the selected policy;
- the ability to create the intended filesystem mount layout;
- the availability and delegation of cgroups v2 controllers required for CPU, memory and PID restrictions;
- the availability of any required seccomp and additional security mechanisms;
- potential restrictions imposed by host settings, including AppArmor policies.

Report granular capability states in the UI and logs. A successful binary presence check does not by itself establish that isolation is operational.

### ER-BB-001.4 — Enforcement and Failure Behaviour

- The **Execution Runtime** must construct and validate the effective isolation policy *before* launching an executable node or service.
- If the required restrictions cannot be applied, **fail closed**: refuse the launch with a clear diagnostic message.
- Never silently fall back to an unrestricted/native execution backend for an isolation-required workload.
- An unrestricted execution profile, if supported, must be explicitly selected and authorized by the user according to QAVector policy.
- Child processes and subprocesses must remain subject to the applicable isolation restrictions.
- Do not expose host file descriptors, privileged sockets, sensitive environment variables or unrestricted host tools to isolated workloads.
- Never rely solely on agent prompts or plugin-level conventions for security enforcement.

### ER-BB-001.5 — Backend Abstraction

Provide a stable execution backend interface, with at least:

- `BundledBubblewrapBackend` — preferred supported Linux isolation backend;
- `SystemBubblewrapBackend` — optional, explicit fallback for compatible system installations;
- `NativeProcessBackend` — only for intentionally non-isolated execution profiles.

Backend selection must not weaken the requested security policy. The Cycle Engine, node plugins and service plugins should not construct Bubblewrap CLI arguments directly.

### ER-BB-001.6 — Resource and Network Boundaries

- Use Bubblewrap/Linux namespaces for filesystem and process isolation where supported.
- Apply CPU, memory and PID limits through supported **cgroups v2** control paths; Bubblewrap alone is not a resource quota system.
- Implement fine-grained network access rules through a separate network policy mechanism when required; Bubblewrap network namespaces alone do not provide hostname/port allowlists.
- Ensure credentials for external tools (Git, Jira, cloud-based agents, etc.) are scoped separately from filesystem access.

## 4. Suggested Distribution Layout

```text
QAVector/
├── qavector
├── runtime/
│   ├── bin/
│   │   └── bwrap
│   ├── lib/                 # Only if bundled dynamic libraries are needed
│   ├── manifests/
│   │   ├── versions.json
│   │   └── checksums.json
│   └── profiles/
│       ├── project-readonly.yaml
│       ├── project-development.yaml
│       └── isolated-service.yaml
└── plugins/
```

The layout is illustrative; use platform-aware paths compatible with the chosen DEB/AppImage packaging model.

## 5. Acceptance Criteria

1. On a clean supported Linux installation, QAVector can run an isolated test workload **without a separately installed system `bubblewrap` package**.
2. The bundled executable is selected and version-checked independently of `PATH`.
3. A node with read/write access only to Project A cannot read or modify Project B, home secrets or unrelated host locations.
4. A service with a read-only mount cannot change files through that mount.
5. Process and subprocess isolation is enforced for executable Cycle nodes, including `git`, `command.shell`, agents and scripts.
6. Network isolation is verified by an integration test; fine-grained allowlists are not claimed unless an actual enforcement mechanism is implemented.
7. Where required CPU, RAM or PID limits are unavailable, QAVector refuses the execution rather than silently ignoring them.
8. Environments and Cycle policies remain effective regardless of the selected plugin or executable node type.
9. Unsupported kernel/security configurations generate actionable diagnostics, without falling back to insecure execution.
10. Build pipelines publish the Bubblewrap version, checksums, applicable licenses and documented host compatibility requirements.

## 6. Implementation Guidance

1. Audit the existing QAVector execution and packaging architecture before introducing the bundled backend.
2. Add a `RuntimeCapabilityProbe` and typed failure reasons.
3. Implement `BundledBubblewrapBackend` behind the Execution Runtime interface.
4. Integrate policy resolution for both Service Isolation and Cycle Node Isolation.
5. Add cgroups v2 and network-policy adapters where needed.
6. Add installation and negative-security integration tests on target Linux environments.
7. Update the Environments GUI to display backend status, host compatibility and enforced restrictions.

## 7. Non-Goals and Constraints

- Bundling Bubblewrap does **not** remove dependencies on Linux kernel functionality, host security configuration or necessary runtime libraries.
- This requirement does not promise that all Linux distributions, kernel configurations or installation formats are automatically supported.
- Bubblewrap is not a substitute for a full virtual machine and is not by itself a complete defense against kernel vulnerabilities.
- Do not replace existing QAVector functionality or disrupt current service and cycle execution while introducing the new backend.
