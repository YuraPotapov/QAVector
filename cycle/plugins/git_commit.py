"""Committing a tree somebody verified, and only that tree.

``git.checkout`` clones into the run and refuses to write anywhere else, which
is right for a step that fetches. This one writes into a repository the user
keeps, so it is a separate plugin: the cycle file says plainly which of the two
a step is, the way ``agent.edit`` is separate from ``agent.review``.

**The point is the check, not the commit.** Committing is one command; anybody
can write it in ``command.shell``, and the demo cycles do. What a shell step
cannot do is answer the two questions that make an automated commit
trustworthy:

* *Is this the tree that passed?* ``expect_tree`` comes from
  ``agent.implement``, which recorded the hash at the moment the checks agreed.
  If anything touched the files since - another step, an editor left open, a
  watcher - the hash differs and the step refuses rather than committing
  something nothing verified.
* *Did I already do this?* A run killed between the commit and the record
  leaves a commit nobody knows about. Re-running would make a second one. So
  the intent is written into the commit message as a trailer, and the next
  attempt looks for it before committing: found, it reports the existing commit
  and says it created nothing. The intent names the *tree and the message* and
  not the parent - the parent is the one thing committing changes, so an id
  that included it would never match on the run that needed it to.

**No state store is involved in either.** The tree hash travels through the
run's own outputs; the operation id lives in the commit message, which is the
one place that survives exactly as long as the commit does. A store would have
to be kept in step with the repository, and the failure mode of a store that
disagrees with git is worse than having no store.

**What it does not do.** No push, no branch creation, no merge, no tag. Those
reach other people, and a step that reaches other people deserves to be asked
for by name rather than arriving as a flag on this one.
"""

import hashlib
import os
import re
import subprocess
import tempfile

from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output

#: The trailer that makes a commit findable again after a crash. Git keeps
#: trailers verbatim, and `git log --grep` finds them without parsing.
TRAILER = "QAVector-Operation"

#: How long any one git command may take. Generous for a large repository,
#: bounded so a hook waiting on input cannot hang a run for ever.
TIMEOUT = 120

#: A commit id, as git prints it. Checked rather than trusted: this value is
#: reported as fact and travels into later steps.
SHA = re.compile(r"^[0-9a-f]{7,64}$")


class GitError(Exception):
    """A git command failed. Carries what git itself said."""


class GitCommit(CyclePlugin):
    metadata = PluginMetadata(
        id="git.commit", name="Git Commit", category=registry.ACTION,
        summary="Commit a verified tree locally, and refuse anything else. "
                "No push.",
        permissions=("process.spawn", "filesystem.read", "filesystem.write"),
        inputs=(
            field("directory", "Repository", "dir", required=True,
                  hint="Relative paths are read against the run's workspace; "
                       "an absolute path commits in that repository."),
            field("message", "Commit message", "multiline", required=True,
                  hint="The first line is the subject. A trailer naming this "
                       "operation is appended, which is how a re-run finds a "
                       "commit it already made."),
            field("expect_tree", "Tree that was verified",
                  hint="From ${steps.<id>.outputs.tree}. The step refuses if "
                       "the files have changed since. Blank commits whatever "
                       "is there now, which is worth being deliberate about."),
            field("expect_branch", "Required branch",
                  hint="When set, refuse unless the current branch matches "
                       "exactly. Use ${steps.todo.outputs.key} for a task branch."),
            field("add", "What to stage", "args",
                  hint="Paths, relative to the repository. Blank stages every "
                       "change, which is what a verified tree means."),
            field("author_name", "Author name",
                  hint="Blank uses the repository's own configuration."),
            field("author_email", "Author email"),
            field("allow_empty", "Commit with no changes", "check",
                  default=False,
                  hint="Off means a step with nothing to commit says so and "
                       "does not make an empty commit."),
        ),
        outputs=(
            output("commit", "text", "The commit id."),
            output("parent", "text", "What it was committed on top of."),
            output("tree", "text", "The tree it actually has."),
            output("branch", "text", "The branch it landed on, or HEAD."),
            output("changed_files", "list", "What the commit touches."),
            output("created", "boolean",
                   "False when this run found its own earlier commit instead "
                   "of making a second one."),
            output("operation_id", "text",
                   "What the trailer says, so a later step can find it too."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        for key in ("directory", "message", "expect_tree", "expect_branch", "author_name",
                    "author_email"):
            if key in settings and not isinstance(settings[key], str):
                found.append("%s must be text." % key)
        if "expect_branch" in settings and not str(settings["expect_branch"] or "").strip():
            found.append("Required branch must not be empty when specified.")
        paths = settings.get("add", [])
        if not (isinstance(paths, str) and "${" in paths):
            if not isinstance(paths, list) or any(not isinstance(one, str)
                                                  for one in paths):
                found.append("What to stage must be a list of paths.")
        tree = str(settings.get("expect_tree") or "")
        if tree and "${" not in tree and not SHA.match(tree.strip()):
            found.append("Tree that was verified does not look like a git "
                         "object id.")
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()

        where = os.path.realpath(os.path.join(
            context.workspace,
            os.path.expanduser(str(self.setting(step.settings, "directory")))))
        if not os.path.isdir(where):
            return registry.failed("Repository does not exist: %s" % where)
        try:
            return self._commit(context, step, where)
        except GitError as exc:
            return registry.failed(str(exc))

    def _commit(self, context, step, where):
        if _git(where, ["rev-parse", "--git-dir"]) is None:
            return registry.failed("Not a git repository: %s" % where)
        branch = self.setting(step.settings, "expect_branch", "")
        _require_branch(where, branch)

        # -- intent ----------------------------------------------------------
        # Hashed against a temporary index, so a step that goes on to refuse
        # leaves the repository exactly as it found it. Staging and then
        # refusing would hand somebody back a staged index they never asked
        # for, and they would have to work out what did it.
        parent = _git(where, ["rev-parse", "HEAD"]) or ""
        paths = self.setting(step.settings, "add", [])
        tree = _would_be(where, paths)
        context.cancel.raise_if_set()

        expected = str(self.setting(step.settings, "expect_tree", "") or "").strip()
        if expected and expected != tree:
            # Refused, not committed. Something touched the files between the
            # checks agreeing and this step running, and whatever is here now
            # is a thing nobody verified.
            return registry.failed(
                "The files changed after they were verified: the step was told "
                "to commit %s and the repository now hashes to %s. Nothing was "
                "committed." % (expected[:12], tree[:12]))

        message = str(self.setting(step.settings, "message") or "").strip()
        operation = _operation_id(tree, message)

        # -- already done? ----------------------------------------------------
        found = _find(where, operation)
        if found:
            return self._receipt(context, where, found, operation, created=False,
                                 message="git.commit: %s was already made"
                                         % found[:12])

        if not parent and not tree:
            return registry.failed("Nothing to commit and no history to add to.")
        empty = bool(self.setting(step.settings, "allow_empty", False))
        if not empty and parent and tree == _git(where, ["rev-parse", "HEAD^{tree}"]):
            # Not a failure: the work was already in the tree. The step says so
            # and a later step can branch on `created`.
            return self._receipt(context, where, parent, operation, created=False,
                                 message="git.commit: nothing to commit")

        # -- effect ------------------------------------------------------------
        # Only now is the real index touched, and only after checking it came
        # out the way the temporary one did.
        _require_branch(where, branch)
        _stage(where, paths)
        staged = _need(where, ["write-tree"], "cannot hash the index")
        if staged != tree:
            return registry.failed(
                "The files changed while the step was staging them: %s became "
                "%s. Nothing was committed." % (tree[:12], staged[:12]))

        argv = ["commit", "-m", "%s\n\n%s: %s" % (message, TRAILER, operation)]
        if empty:
            argv.append("--allow-empty")
        environment = _author(self.setting(step.settings, "author_name", ""),
                              self.setting(step.settings, "author_email", ""))
        _require_branch(where, branch)
        _need(where, argv, "the commit was refused", environment)

        # -- reconcile ---------------------------------------------------------
        made = _need(where, ["rev-parse", "HEAD"], "cannot read HEAD back")
        actual = _need(where, ["rev-parse", "HEAD^{tree}"],
                       "cannot read the commit's tree")
        if actual != tree:
            # A hook rewrote the files. The commit exists and says something
            # nobody checked, so the step fails with its id rather than
            # committing again on top.
            return registry.PluginResult(
                "failed",
                outputs={"commit": made, "parent": parent, "tree": actual,
                         "created": True, "operation_id": operation,
                         "branch": _git(where, ["rev-parse", "--abbrev-ref", "HEAD"]) or ""},
                message="Commit %s has tree %s, not the verified %s - something "
                        "rewrote the files as it was made, most likely a hook. "
                        "The commit is there; it is not the one that was "
                        "verified." % (made[:12], actual[:12], tree[:12]))
        return self._receipt(context, where, made, operation, created=True,
                             message="git.commit: %s" % made[:12])

    def _receipt(self, context, where, commit, operation, created, message):
        """What was actually committed, read back from git rather than assumed."""
        return registry.PluginResult(
            "success",
            outputs={
                "commit": commit,
                "parent": _git(where, ["rev-parse", commit + "^"]) or "",
                "tree": _git(where, ["rev-parse", commit + "^{tree}"]) or "",
                "branch": _git(where, ["rev-parse", "--abbrev-ref", "HEAD"]) or "",
                "changed_files": _changed(where, commit),
                "created": created,
                "operation_id": operation,
            },
            message=message)


# -- talking to git -----------------------------------------------------------
def _require_branch(where, expected):
    """Check the actual HEAD, including when this step is run on its own."""
    if not expected:
        return
    actual = _git(where, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if actual != expected:
        raise GitError("Branch mismatch: expected task branch %r, current branch "
                       "is %r. Nothing was committed."
                       % (expected, actual or "detached HEAD"))


def _operation_id(tree, message):
    """A stable name for this intent: what is being committed, and why.

    Derived from what is being committed rather than minted fresh, so the same
    intent computes the same id on a re-run - which is the whole mechanism by
    which a killed run finds its own commit instead of making a second.

    **Deliberately not including the parent.** The parent is the one thing the
    commit itself changes: a run killed after committing sees a different HEAD
    next time, so an id that included it would be a different id every time and
    would never find anything - which is exactly the case it exists for.
    """
    seed = "\n".join([tree or "", message or ""])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _find(where, operation):
    """The commit carrying this operation's trailer, if one is already here."""
    found = _git(where, ["log", "--format=%H", "-n", "20",
                         "--grep=%s: %s" % (TRAILER, operation)])
    if not found:
        return ""
    first = found.splitlines()[0].strip()
    return first if SHA.match(first) else ""


def _would_be(where, paths):
    """The tree a commit would have, computed without touching the real index.

    Seeded from HEAD so unchanged files are already accounted for, then the
    work added on top - which is what ``git add`` followed by ``git commit``
    produces, and what ``agent.implement`` hashed when the checks agreed.
    """
    with tempfile.TemporaryDirectory(prefix="qavector-commit-") as scratch:
        index = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        _git(where, ["read-tree", "HEAD"], index)    # no HEAD yet is fine
        _stage(where, paths, index)
        return _need(where, ["write-tree"], "cannot hash the changes", index)


def _stage(where, paths, extra=None):
    """Put the work in the index. Everything, unless the step named paths."""
    argv = ["add", "--"] + list(paths) if paths else ["add", "-A"]
    _need(where, argv, "cannot stage the changes", extra)


def _changed(where, commit):
    """What one commit touches. Empty for a root commit git cannot diff."""
    found = _git(where, ["show", "--name-only", "--format=", commit])
    if found is None:
        return []
    return [one.strip() for one in found.splitlines() if one.strip()]


def _author(name, email):
    """The environment that sets the author, or none when the repo decides."""
    environment = {}
    if str(name or "").strip():
        environment["GIT_AUTHOR_NAME"] = environment["GIT_COMMITTER_NAME"] = name
    if str(email or "").strip():
        environment["GIT_AUTHOR_EMAIL"] = environment["GIT_COMMITTER_EMAIL"] = email
    return environment


def _run(where, arguments, extra=None):
    """One git command. Returns (code, stdout, stderr)."""
    environment = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        environment.pop(key, None)
    # The same sanitising git.checkout does: no prompt, no user config leaking
    # into what a run produces.
    environment.update({"GIT_TERMINAL_PROMPT": "0"})
    environment.update(extra or {})
    try:
        done = subprocess.run(["git"] + list(arguments), cwd=where,
                              env=environment, capture_output=True, text=True,
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GitError("git %s did not finish within %ds"
                       % (arguments[0], TIMEOUT))
    except OSError as exc:
        raise GitError("cannot run git: %s" % exc)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def _git(where, arguments, extra=None):
    """One git query. ``None`` when git refused, so a caller can decide."""
    code, out, _err = _run(where, arguments, extra)
    return out if code == 0 else None


def _need(where, arguments, why, extra=None):
    """One git command that has to work. Raises :class:`GitError` with its own
    words, because git's message is almost always the useful one."""
    code, out, err = _run(where, arguments, extra)
    if code != 0:
        raise GitError("%s: %s" % (why, err or out or "git exited %d" % code))
    return out
