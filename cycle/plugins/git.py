"""An isolated Git checkout for a cycle, without modifying the source tree."""

import os
import re
import shutil
import stat
import tempfile

from cycle import registry
from cycle.plugins._process import run_process
from cycle.registry import CyclePlugin, PluginMetadata, field, output


class GitCheckout(CyclePlugin):
    metadata = PluginMetadata(
        id="git.checkout", name="Git Checkout", category=registry.ACTION,
        summary="Clone a repository into this run and select a branch or commit.",
        permissions=("process.spawn", "filesystem.read", "filesystem.write",
                     "network"),
        inputs=(
            field("repository", "Repository", required=True,
                  hint="Git URL or local path. Relative paths use the run workspace."),
            field("branch", "Branch or tag",
                  hint="Blank uses the remote's default branch."),
            field("commit", "Commit",
                  hint="Optional commit or revision to check out with detached HEAD."),
            field("path", "Destination", "dir",
                  hint="New directory inside the run workspace. Blank uses source/<step>."),
            field("depth", "Clone depth", "number", default=0,
                  hint="0 keeps full history. A shallow clone may not contain an older commit."),
            field("submodules", "Initialize submodules", "check", default=False),
        ),
        outputs=(
            output("workspace", "string", "Checkout path relative to the run workspace."),
            output("commit", "string", "The full checked-out commit id."),
            output("branch", "string", "Checked-out branch, or HEAD for a detached commit."),
        ),
    )

    def problems(self, settings):
        settings = settings or {}
        literal = dict(settings)
        if isinstance(literal.get("depth"), str) and "${" in literal["depth"]:
            literal.pop("depth")
        found = super().problems(literal)
        for key in ("repository", "branch", "commit", "path"):
            value = settings.get(key)
            if value is not None and not isinstance(value, str):
                found.append("%s must be text." % key)
        submodules = settings.get("submodules", False)
        if not isinstance(submodules, bool) and not (
                isinstance(submodules, str) and "${" in submodules):
            found.append("Initialize submodules must be a boolean.")
        depth = settings.get("depth", 0)
        if not (isinstance(depth, str) and "${" in depth):
            try:
                if isinstance(depth, bool) or int(depth) < 0 or float(depth) != int(depth):
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                found.append("Clone depth must be a non-negative integer.")
        return found

    def execute(self, context, step):
        problems = self.problems(step.settings)
        if problems:
            return registry.failed("; ".join(problems))
        root = os.path.realpath(context.workspace)
        destination = self.setting(step.settings, "path") or os.path.join("source", step.id)
        requested = os.path.abspath(os.path.join(root, os.path.expanduser(destination)))
        target = os.path.realpath(requested)
        try:
            inside = os.path.commonpath([root, target]) == root and target != root
        except ValueError:
            inside = False
        if not inside:
            return registry.failed("Git destination must be inside the run workspace.")
        if os.path.lexists(requested) or os.path.lexists(target):
            return registry.failed("Git destination already exists: %s" % context.relative(target))
        if context.cancel.is_set():
            return registry.failed("cancelled before checkout")

        os.makedirs(os.path.dirname(target), exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".checkout-", dir=os.path.dirname(target))
        artifacts = []

        def git(name, args, directory=root):
            # Do not inherit Git's repository/index overrides from the launcher.
            # Zero GIT_CONFIG_COUNT disables injected -c pairs; normal user
            # configuration and credential helpers still work.
            env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "0"}
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
                env[key] = None
            result = run_process(context, step, ["git"] + args, directory, name, env)
            artifacts.extend(result.artifacts)
            result.artifacts = list(artifacts)
            return result

        try:
            repository = os.path.expanduser(self.setting(step.settings, "repository"))
            branch = self.setting(step.settings, "branch", "")
            commit = self.setting(step.settings, "commit", "")
            args = ["clone", "--no-hardlinks", "--dissociate"]
            if branch:
                args.append("--branch=" + branch)
            if commit:
                args.append("--no-checkout")
            depth = int(self.setting(step.settings, "depth", 0))
            if depth:
                args.append("--depth=%d" % depth)
            result = git("clone", args + ["--", repository, staging])
            if not result.ok:
                return result
            if commit:
                result = git("resolve", ["rev-parse", "--verify", "--end-of-options",
                                         commit + "^{commit}"], staging)
                if not result.ok:
                    return result
                revision = result.outputs["stdout_tail"].strip()
                if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
                    return registry.PluginResult("failed", artifacts=artifacts,
                                                 message="Git returned an invalid commit id.")
                result = git("checkout", ["checkout", "--detach", revision], staging)
                if not result.ok:
                    return result
            if self.setting(step.settings, "submodules", False):
                result = git("submodules", ["submodule", "update", "--init", "--recursive"], staging)
                if not result.ok:
                    return result
            result = git("revision", ["rev-parse", "HEAD"], staging)
            if not result.ok:
                return result
            revision = result.outputs["stdout_tail"].strip()
            result = git("branch", ["rev-parse", "--abbrev-ref", "HEAD"], staging)
            if not result.ok:
                return result
            checked_branch = result.outputs["stdout_tail"].strip()
            if context.cancel.is_set():
                return registry.PluginResult("failed", artifacts=artifacts,
                                             message="checkout was stopped")
            os.rename(staging, target)
            return registry.PluginResult(
                "success", outputs={"workspace": context.relative(target),
                                    "commit": revision, "branch": checked_branch},
                artifacts=artifacts, message="checked out %s" % revision[:12])
        except OSError as exc:
            return registry.PluginResult("failed", artifacts=artifacts,
                                         message="checkout failed: %s" % exc)
        finally:
            # Only the temporary directory created by this invocation is removed.
            # Failed attempts leave the destination free for the engine's retry.
            if os.path.isdir(staging):
                shutil.rmtree(staging, onerror=_remove_readonly)


def _remove_readonly(remove, path, _error):
    """Git object files can be read-only on Windows, including failed clones."""
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
    remove(path)
