"""Prepare an existing checkout's task branch from freshly fetched remote refs.

**When there is nothing to fetch.** Fetching is the point of this step - the
task branch is meant to start from the base as it is now, not as it was - so a
failed fetch stops it. But "nothing came from the remote" is not the same fact
as "this repository is in no state to work in": on a laptop off the VPN, or a
network that drops for a minute, there is a perfectly good base branch sitting
in the checkout already. With ``offline`` set, the step says so in its output
and prepares the branch from what is already here, naming the commit it used so
nobody has to wonder how old it is. Without it, nothing changes.

**A checkout with no remotes at all** carries on that way whatever the settings
say. It used to be refused outright, before the step reached any of the above -
which was the wrong answer twice over: there is nothing unreachable about a
repository that was never given a remote, and nothing misconfigured either. The
one thing that makes a directory unworkable here is not being a git checkout.
A remote that is *named* and does not exist is different - a typo should not
quietly build on a stale base - so that follows ``offline`` like a failed fetch.
"""

import os

from cycle import registry
from cycle.plugins._process import run_process
from cycle.plugins.git_commit import GitError, _git, _need, _require_branch
from cycle.registry import CyclePlugin, PluginMetadata, field, output


class GitPrepareBranch(CyclePlugin):
    metadata = PluginMetadata(
        id="git.prepare_branch", name="Prepare Task Branch", category=registry.ACTION,
        summary="Find or create the task branch and merge the latest remote base into it.",
        permissions=("process.spawn", "filesystem.read", "filesystem.write", "network"),
        inputs=(
            field("directory", "Repository", "dir", required=True),
            field("branch", "Task branch", required=True,
                  hint="Exact task key, for example ${steps.todo.outputs.key}."),
            field("remote", "Remote", default="origin"),
            field("base", "Base branch", default="main",
                  hint="Fetched from the remote on every run. The local branch is only "
                       "used when the remote could not be reached and that is allowed."),
            field("offline", "Carry on when nothing comes from the remote", "check",
                  default=False,
                  hint="Prepare the branch from what this checkout already has - the "
                       "last fetched base if there is one, otherwise the local base "
                       "branch - saying in the output that nothing was fetched and "
                       "which commit was used instead. Covers a remote that will not "
                       "answer and one that is named here but not configured there. A "
                       "checkout with no remotes at all carries on without this. "
                       "Everything else about the step is unchanged: a dirty tree, a "
                       "wrong branch name or a merge conflict still stops it."),
        ),
        outputs=(
            output("branch", "text", "The task branch now checked out."),
            output("base_commit", "text", "The base commit merged into it."),
            output("commit", "text", "HEAD after preparing the branch."),
            output("created", "boolean", "Whether a local task branch was created."),
            output("offline", "boolean",
                   "Whether nothing was fetched - the remote would not answer, or "
                   "there is none - so the base is as old as this checkout rather "
                   "than as new as the remote."),
            output("base_ref", "text",
                   "Which ref the base commit was read from - the remote-tracking one "
                   "on an ordinary run, a local branch when working offline."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        found = super().problems(settings)
        for key in ("directory", "branch", "remote", "base"):
            value = self.setting(settings, key)
            if not isinstance(value, str) or not value.strip():
                found.append("%s must be non-empty text." % key)
        offline = settings.get("offline", False)
        if not isinstance(offline, bool) and str(offline).strip().lower() not in (
                "", "true", "false", "yes", "no", "on", "off", "1", "0"):
            # Refused here rather than read as "off": a value nobody can spell
            # an intention out of is a line somebody meant something by.
            found.append("Carry on when the remote will not answer must be a boolean.")
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        context.cancel.raise_if_set()
        where = os.path.realpath(os.path.join(
            context.workspace, os.path.expanduser(self.setting(step.settings, "directory"))))
        if not os.path.isdir(where):
            return registry.failed("Repository does not exist: %s" % where)
        try:
            return self._prepare(context, step, where)
        except GitError as exc:
            return registry.failed(str(exc))

    def _base(self, where, remote, base, offline):
        """``(ref, commit)`` for the base this run starts the branch from.

        Online there is one answer and a missing one is a failure. Offline the
        last fetched base is tried first - it is the same ref an ordinary run
        would use, only older - and the local branch after it, because a
        checkout that has never fetched this base still has the branch somebody
        has been working from. Both named in the output, so "how old is this"
        is a question the run answers rather than raises.
        """
        tracking = "refs/remotes/%s/%s" % (remote, base)
        if not offline:
            return tracking, _need(
                where, ["rev-parse", "--verify", tracking + "^{commit}"],
                "remote base branch %s/%s is missing" % (remote, base))
        for ref in (tracking, "refs/heads/" + base):
            commit = _git(where, ["rev-parse", "--verify", ref + "^{commit}"])
            if commit:
                return ref, commit
        raise GitError(
            "Nothing was fetched from %s and this checkout has no base to "
            "start from: neither %s nor the local branch %s exists."
            % (remote, tracking, base))

    def _prepare(self, context, step, where):
        branch = self.setting(step.settings, "branch")
        base = self.setting(step.settings, "base")
        remote = self.setting(step.settings, "remote")
        if _git(where, ["rev-parse", "--is-inside-work-tree"]) != "true":
            raise GitError("Not a git working checkout: %s" % where)
        for label, name in (("Task branch", branch), ("Base branch", base)):
            if (name.startswith("-") or name == "HEAD"
                    or _git(where, ["check-ref-format", "refs/heads/" + name]) is None):
                raise GitError("%s is not a valid branch name: %r" % (label, name))
        if branch == base:
            raise GitError("Task branch must differ from the base branch %r." % base)
        if remote.startswith("-"):
            raise GitError("Remote is not a valid name: %r" % remote)
        remotes = _need(where, ["remote"], "cannot list remotes").splitlines()
        if _need(where, ["status", "--porcelain", "--untracked-files=all"],
                 "cannot check the working tree"):
            raise GitError("Repository has uncommitted changes. Commit or stash them "
                           "before preparing the task branch; no files were changed.")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                       "rebase-merge", "rebase-apply", "sequencer"):
            path = _need(where, ["rev-parse", "--git-path", marker],
                         "cannot check repository state")
            if os.path.exists(os.path.join(where, path)):
                raise GitError("An unfinished Git operation exists (%s). "
                               "Finish it before preparing the task branch." % marker)

        artifacts = []
        environment = {"GIT_TERMINAL_PROMPT": "0", "GIT_MERGE_AUTOEDIT": "no",
                       "GIT_CONFIG_COUNT": "0"}
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            environment[key] = None

        def run(name, arguments):
            result = run_process(context, step, ["git"] + arguments, where,
                                 name, environment)
            artifacts.extend(result.artifacts)
            result.artifacts = list(artifacts)
            return result

        offline = False
        if remote not in remotes:
            # A remote that is not configured is not a remote that will not
            # answer, and until now it was neither: the step refused before it
            # reached the offline path that exists for exactly this - a
            # checkout with a perfectly good base branch in it. The only thing
            # that makes a directory unworkable here is not being a checkout at
            # all, and that is already settled above.
            #
            # The two cases are still told apart. A repository with **no**
            # remotes has nothing to fetch from and nothing misconfigured, so
            # it carries on whatever the settings say. One that has remotes but
            # not this one is a name that matches nothing - `orgin` for
            # `origin` - and building silently on a stale base is the wrong
            # answer to a typo, so that follows `offline` like a failed fetch.
            if remotes and not _wanted(self.setting(step.settings, "offline", False)):
                raise GitError(
                    "No configured remote named %r. This checkout has %s. "
                    "Name one of those, or set offline to prepare the branch "
                    "from what is already here."
                    % (remote, ", ".join(sorted(remotes))))
            offline = True
            context.log(step.id, "stdout", [
                "There is no remote named %r here%s." % (
                    remote, " (this checkout has no remotes at all)"
                    if not remotes else ""),
                "Preparing the branch from what this checkout already has."])
        else:
            # An explicit refspec also works for single-branch clones. Pruning
            # prevents a deleted remote task branch from being reused from cache.
            fetched = run("fetch", ["fetch", "--prune", "--no-tags",
                                    "--no-recurse-submodules", remote,
                                    "+refs/heads/*:refs/remotes/%s/*" % remote])
            if not fetched.ok:
                if not _wanted(self.setting(step.settings, "offline", False)):
                    fetched.message = "Cannot fetch current branches from %s: %s" % (
                        remote, fetched.message)
                    return fetched
                offline = True
                context.log(step.id, "stdout", [
                    "%s could not be reached: %s" % (remote, fetched.message),
                    "Preparing the branch from what this checkout already has."])

        base_ref, base_commit = self._base(where, remote, base, offline)
        local = _git(where, ["show-ref", "--verify", "refs/heads/" + branch])
        task_ref = "refs/remotes/%s/%s" % (remote, branch)
        task_commit = _git(where, ["rev-parse", "--verify", task_ref + "^{commit}"])

        if local is not None:
            arguments = ["checkout", "--no-guess", branch]
        elif task_commit:
            arguments = ["checkout", "--track", "-b", branch, task_ref]
        else:
            arguments = ["checkout", "--no-track", "-b", branch, base_commit]
        switched = run("checkout", arguments)
        if not switched.ok:
            return switched
        _require_branch(where, branch)

        # Preserve local task commits while also incorporating work already
        # pushed to the same task branch before merging the production base.
        for name, revision in (("merge-task", task_commit if local is not None else None),
                               ("merge-base", base_commit)):
            if not revision:
                continue
            _require_branch(where, branch)
            merged = run(name, ["merge", "--no-edit", revision])
            if not merged.ok:
                merged.message = "Cannot merge into task branch %s: %s. " \
                    "Resolve any merge conflicts before running the cycle again." % (
                        branch, merged.message)
                return merged
        _require_branch(where, branch)
        return registry.PluginResult(
            "success", outputs={"branch": branch, "base_commit": base_commit,
                                "commit": _need(where, ["rev-parse", "HEAD"], "cannot read HEAD"),
                                "created": local is None,
                                "offline": offline, "base_ref": base_ref},
            artifacts=artifacts,
            message=("nothing was fetched from %s; task branch %s prepared from "
                     "%s (%s), which is as old as this checkout."
                     % (remote, branch, _named(base_ref), base_commit[:12])) if offline
            else "Task branch %s includes current %s/%s (%s)." % (
                branch, remote, base, base_commit[:12]))


def _named(ref):
    """A ref as somebody says it: ``origin/main``, ``main``."""
    for prefix in ("refs/remotes/", "refs/heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _wanted(value):
    """Whether a check field is on, however the file spelt it.

    A form sends a real boolean and a hand-written YAML file sends ``true``,
    ``"yes"`` or ``"1"``. Anything else is off, so a typo leaves the step doing
    what it has always done rather than quietly accepting a stale base.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "on", "1")
