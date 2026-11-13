# QCR — Phase 5.2: context packages

Date: 2026-11-14. Branch: `codex/phase-4-unified-agent-adapter`, on top of 5.1 (`7645088`).
Design: [ADR-006 D7](adr-006-agent-messaging-and-delegation.md).

## Implemented

* `qcr/context.py` builds, keeps, loads, checks freshness of and renders
  packages:
  * content-addressed (`<project>/.qcr/context/<sha256>.json`), owner-only,
    written once;
  * integrity-checked on load: a changed file gives `integrity`.
* `agents.<id>.delegate` takes `files` (paths inside the folder, at most 50)
  and `artifacts` (the delegator's own executions, at most 20). The task is
  packaged with:
  * the file hashes;
  * the git `HEAD`;
  * artifact excerpts;
  * constraints (mode, time, depth 1, no QAVector access);
  * provenance.

  The approval card names the package and how many files and artifacts it
  has. The output carries `context_package` and `stale_files`.
* Paths are resolved through links, so a link out of the folder is refused,
  and so are missing files. Another agent's executions are `not_found`.
* **Secrets:**
  * every value in the secrets store, plus high-confidence credential
    shapes, refuses the task (`secret_in_context`), naming the field, never
    the value;
  * the text fields are checked before the Engine records the request, so a
    refused secret never reaches the journal. A test found this: the first
    version refused the package but had already journaled the request.
* **Freshness:** files are hashed again at launch, after any approval wait. A
  changed file is marked in the child's message and listed in `stale_files`.

## Validation

* `tests/test_qcr_context.py`: **15 passed**. Unit tests cover paths, links,
  git, secrets, limits, immutability, integrity and freshness. Tests through
  the host cover the package told to the child after a file changed during
  approval, artifact ownership, a secret refused with nothing journaled, and
  values from the real secrets store.
* `tests/test_qcr_delegation.py`: **12 passed**.
* Core selection (agents, QCR, journal, packaging, cycle agent connections, plugins and secrets): **880 passed, 1 skipped**.

Not run: a real model turn, the full repository suite.

## Next

5.3 messages, 5.4 GUI (until then nothing in the app delegates), 5.5 the
user's live check.
