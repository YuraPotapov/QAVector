"""Capture the prepared task and Git state, without an agent or external write."""

import html
import json
import os

from cycle import registry, run as records
from cycle.plugins.git_commit import GitError, _git, _need
from cycle.registry import CyclePlugin, PluginMetadata, field, output
from domain.cycle import Artifact


class TaskBrief(CyclePlugin):
    metadata = PluginMetadata(
        id="task.brief", name="Task Brief", category=registry.REPORT,
        summary="Record task, workspace paths and exact Git snapshots after preparation.",
        permissions=("filesystem.read", "filesystem.write", "process.spawn"),
        inputs=(field("issues", "Chosen Jira task", required=True),
                field("directory", "Prepared workspace", "dir", required=True),
                field("source", "Source checkout", "dir", required=True),
                field("base", "Prepared base ref", required=True),
                field("base_update", "Base update result", "text"),
                field("task_report", "Jira reading copy", "text")),
        outputs=(output("brief", "mapping"), output("ready", "boolean"),
                 output("path", "text")),
    )

    def execute(self, context, step):
        settings = step.settings
        problems = self.problems(settings)
        if problems:
            return registry.failed("; ".join(problems))
        issues = settings.get("issues")
        if not isinstance(issues, list) or len(issues) != 1 or not isinstance(issues[0], dict):
            return registry.failed("Task Brief requires exactly one chosen Jira task.")
        issue = issues[0]
        if not issue.get("key"):
            return registry.failed("The chosen task has no key.")
        base = str(settings.get("base") or "").strip()
        if not base or base.startswith("-"):
            return registry.failed("A base ref is required.")
        directory = os.path.realpath(os.path.join(context.workspace, os.path.expanduser(str(settings.get("directory") or ""))))
        source = os.path.realpath(os.path.join(context.workspace, os.path.expanduser(str(settings.get("source") or ""))))
        context.cancel.raise_if_set()
        try:
            head = _need(directory, ["rev-parse", "--verify", "HEAD^{commit}"], "Cannot read prepared HEAD")
            base_commit = _need(directory, ["rev-parse", "--verify", base + "^{commit}"], "Cannot read prepared base")
            branch = _need(directory, ["symbolic-ref", "--quiet", "--short", "HEAD"], "Prepared workspace needs a branch")
            source_head = _need(source, ["rev-parse", "--verify", "HEAD^{commit}"], "Cannot read source checkout HEAD")
            conflicts = (_git(directory, ["diff", "--name-only", "--diff-filter=U"]) or "").splitlines()
            merge_head = _git(directory, ["rev-parse", "--verify", "MERGE_HEAD"])
            contained = _git(directory, ["merge-base", "--is-ancestor", base_commit, head]) is not None
            dirty = _need(directory, ["status", "--porcelain", "--untracked-files=all"],
                          "Cannot check prepared files", {"GIT_OPTIONAL_LOCKS": "0"})
            ready = not conflicts and not merge_head and contained and not dirty
            marker = _need(directory, ["rev-parse", "--git-path", "qavector-prepare.json"], "Cannot locate preparation record")
            workspace = {"path": directory, "source": source, "operation": "not_recorded"}
            try:
                with open(os.path.join(directory, marker), encoding="utf-8") as handle:
                    evidence = json.load(handle)
                if isinstance(evidence, dict) and type(evidence.get("existed_before")) is bool:
                    workspace.update(existed_before=evidence["existed_before"],
                                     operation="recreated" if evidence["existed_before"] else "created")
            except (OSError, ValueError):
                pass
            prior_branch = any(_git(directory, ["rev-parse", "--verify", ref + branch + "^{commit}"])
                               for ref in ("refs/remotes/claim/", "refs/remotes/origin/"))
            brief = {"task": {name: issue.get(name) or "" for name in ("key", "summary", "status", "updated", "url")},
                     "workspace": workspace,
                     "git": {"branch": branch, "head": head, "base_ref": base,
                             "base_update": str(settings.get("base_update") or "not_recorded"),
                             "base_commit": base_commit, "contains_base": contained,
                             "branch_operation": "reused" if prior_branch else "created",
                             "source_head": source_head,
                             "conflicts": conflicts, "merge_in_progress": bool(merge_head),
                             "dirty": bool(dirty)},
                     "ready": ready, "task_report": str(settings.get("task_report") or ""),
                     "external_effects": [] if context.run.action_id == "prepare_task" else None,
                     "effects_verification": "declared" if context.run.action_id == "prepare_task" else "not_recorded"}
        except GitError as exc:
            return registry.failed(str(exc))
        context.cancel.raise_if_set()
        path = os.path.join(context.step_dir(step.id), "task-brief.json")
        records._atomic(path, brief)
        page = os.path.join(context.step_dir(step.id), "task-brief.html")
        rows = [("Task", "%s · %s" % (issue["key"], issue.get("summary") or "")),
                ("Jira status", brief["task"]["status"]),
                ("Workspace", directory), ("Workspace operation", workspace["operation"]),
                ("Source checkout", source), ("Branch", branch), ("HEAD", head),
                ("Base ref", base), ("Base SHA", base_commit),
                ("External effects", "None declared" if brief["external_effects"] == [] else "Not recorded"),
                ("Ready", str(ready)), ("Conflicts", ", ".join(conflicts))]
        with open(page, "w", encoding="utf-8") as handle:
            handle.write("<!doctype html><meta charset='utf-8'><title>Task Brief</title><h1>Task Brief</h1><dl>" +
                         "".join("<dt>%s</dt><dd>%s</dd>" % (html.escape(k), html.escape(str(v))) for k, v in rows) + "</dl>")
        artifacts = [Artifact(kind, context.relative(p), step.id, name="Task Brief", bytes=os.path.getsize(p))
                     for kind, p in (("json", path), ("html", page))]
        return registry.PluginResult("success" if ready else "failed", outputs={
            "brief": brief, "ready": ready, "path": context.relative(page)}, artifacts=artifacts,
            message="Task prepared" if ready else "Preparation blocked: base is not merged or conflicts remain.")
