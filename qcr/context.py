"""Context packages: what one agent hands another with a task (ADR-006 D7).

A package is an immutable JSON document, content-addressed: its file name is
the SHA-256 of its canonical bytes, in ``<project>/.qcr/context/``. It carries
what the receiving agent needs and nothing of the sender's conversation:

* the goal, acceptance criteria, constraints and a summary the sender wrote;
* files, by path inside the folder, each with its SHA-256 and size at packaging
  time, and the folder's git ``HEAD`` when it is a repository;
* artifacts - the sender's own executions - each with its capability, state
  and a bounded excerpt of its output, since the receiver cannot read them
  itself (it has no QAVector access);
* provenance: who packaged it, from which conversation, when.

Nothing that looks like a secret goes in. A value from the secrets store, or
text matching one of :data:`SECRET_PATTERNS`, refuses the package and names the
field, never the value. A package larger than :data:`MAX_BYTES` is refused.
:func:`freshness` says which files changed since packaging, so a task that
waited for approval tells its agent so.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time

from journal import canonical

from .errors import QCRError

VERSION = 1
MAX_BYTES = 64 * 1024
MAX_FILES, MAX_ARTIFACTS = 50, 20
EXCERPT_CHARS = 2000
#: Shorter known secrets are not looked for: they would match ordinary words.
MIN_SECRET_CHARS = 8

#: High-confidence shapes of credentials. A false positive refuses a package,
#: which the sender can rephrase; a false negative would hand a key on.
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)

_REF = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_head(folder):
    """The commit checked out in ``folder``'s repository, or None."""
    try:
        done = subprocess.run(["git", "-C", folder, "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    head = done.stdout.strip()
    return head if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head) else None


def _inside(folder, relative):
    """``relative`` as an absolute path inside ``folder``, or refused."""
    if not isinstance(relative, str) or not relative.strip() or "\0" in relative:
        raise QCRError("invalid_input", "A file is a path inside the conversation's folder.")
    root = os.path.realpath(folder)
    path = os.path.realpath(os.path.join(root, relative))
    if path != root and not path.startswith(root + os.sep):
        raise QCRError("invalid_input", "A file must be inside the conversation's folder.")
    if not os.path.isfile(path):
        raise QCRError("invalid_input", "A file to hand on is not there: %s" % relative[:200])
    return path, os.path.relpath(path, root)


def refuse_secrets(fields, known_secrets):
    """Raise ``secret_in_context`` for the first of ``fields`` (``(name, value)``)
    that holds a known secret or matches :data:`SECRET_PATTERNS`."""
    secrets = [one for one in known_secrets if isinstance(one, str) and len(one) >= MIN_SECRET_CHARS]
    for name, value in fields:
        text = value if isinstance(value, str) else json.dumps(value)
        if any(secret in text for secret in secrets) or any(p.search(text) for p in SECRET_PATTERNS):
            raise QCRError("secret_in_context", "The %s looks like it holds a secret; it was not "
                                                "handed on. Leave the secret out." % name)


def build(*, goal, criteria, summary, constraints, folder, files=(), artifacts=(), provenance,
          known_secrets=(), clock=time.time):
    """A package document. ``artifacts`` are executions as the store holds them,
    already checked to be the sender's. Raises :class:`QCRError`."""
    files, artifacts = list(files or ()), list(artifacts or ())
    if len(files) > MAX_FILES or len(artifacts) > MAX_ARTIFACTS:
        raise QCRError("limit_exceeded", "A task takes at most %d files and %d artifacts."
                       % (MAX_FILES, MAX_ARTIFACTS))
    now = clock()
    listed, seen = [], set()
    for relative in files:
        path, shown = _inside(folder, relative)
        if shown in seen:
            continue
        seen.add(shown)
        listed.append({"path": shown, "sha256": _sha256_file(path), "bytes": os.path.getsize(path)})
    handed = []
    for execution in artifacts:
        output = execution.get("output")
        excerpt = "" if output is None else json.dumps(output, ensure_ascii=False, sort_keys=True)
        handed.append({"execution_id": execution["execution_id"], "capability": execution["capability_id"],
                       "state": execution["state"], "output_excerpt": excerpt[:EXCERPT_CHARS],
                       "truncated": len(excerpt) > EXCERPT_CHARS})
    refuse_secrets([("goal", goal), ("acceptance criteria", criteria), ("summary", summary),
                  ("file list", [one["path"] for one in listed]),
                  ("artifacts", [one["output_excerpt"] for one in handed])], known_secrets)
    document = {"version": VERSION, "goal": goal, "acceptance_criteria": list(criteria),
                "summary": summary, "constraints": dict(constraints), "files": listed,
                "git": {"head": git_head(folder)}, "artifacts": handed,
                "provenance": dict(provenance, packaged_at=now)}
    if len(canonical(document).encode("utf-8")) > MAX_BYTES:
        raise QCRError("limit_exceeded", "The task's context is larger than %d KB; hand on less."
                       % (MAX_BYTES // 1024))
    return document


def reference(document):
    return hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()


def keep(document, directory):
    """Write ``document`` once, owner-only, under its reference. Returns the reference."""
    ref = reference(document)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, ref + ".json")
    if os.path.exists(path):
        return ref                      # immutable: the same bytes are already there
    descriptor, temporary = tempfile.mkstemp(prefix=".package-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical(document))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return ref


def load(ref, directory):
    """A kept package, checked against its reference. Raises :class:`QCRError`."""
    if not isinstance(ref, str) or not _REF.match(ref):
        raise QCRError("invalid_input", "A context package is named by its SHA-256.")
    try:
        with open(os.path.join(directory, ref + ".json"), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        raise QCRError("not_found", "That context package is not here.") from None
    if reference(document) != ref:
        raise QCRError("integrity", "That context package was changed after it was made.")
    return document


def freshness(document, folder):
    """The package's files that changed (or went) since it was made."""
    changed = []
    root = os.path.realpath(folder)
    for one in document.get("files") or []:
        path = os.path.join(root, one["path"])
        try:
            if _sha256_file(path) != one["sha256"]:
                changed.append(one["path"])
        except OSError:
            changed.append(one["path"])
    return changed


def render(document, *, sender, mode, changed=(), closing=""):
    """The receiving agent's message: the package as text."""
    changed = set(changed)
    lines = ["[QAVector: a task delegated to you by %s]" % sender, "", "Goal: " + document["goal"]]
    if document["acceptance_criteria"]:
        lines += ["", "Acceptance criteria:"] + ["%d. %s" % (n, one) for n, one in
                                                  enumerate(document["acceptance_criteria"], 1)]
    if document["summary"]:
        lines += ["", "What you need to know:", document["summary"]]
    if document["files"]:
        head = (document.get("git") or {}).get("head")
        lines += ["", "Files to look at%s:" % (" (repository at %s)" % head[:12] if head else "")]
        for one in document["files"]:
            note = " (changed since the task was given)" if one["path"] in changed else ""
            lines.append("- %s (sha256 %s, %d bytes)%s" % (one["path"], one["sha256"][:12], one["bytes"], note))
    if document["artifacts"]:
        lines += ["", "What was already done (the delegating agent's runs):"]
        for one in document["artifacts"]:
            lines.append("- %s: %s" % (one["capability"], one["state"]))
            if one["output_excerpt"]:
                lines.append("  output: " + one["output_excerpt"] + (" ..." if one["truncated"] else ""))
    lines += ["", "Mode: %s." % mode]
    if closing:
        lines.append(closing)
    return "\n".join(lines)
