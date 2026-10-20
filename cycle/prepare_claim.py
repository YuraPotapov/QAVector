"""Upgrade the existing Claim cycle for Prepare task, preserving its comments.

Create a reviewable draft with --source FILE --output FILE. --apply writes that
draft back only when the source digest still matches --expected-digest.
"""

import argparse
import copy
import os
import stat
import tempfile

from cycle import cyclefile, cyclepatch, model, registry

PREPARE_STEPS = ["queue", "pick", "todo", "claim", "spent", "admit", "workspace",
                 "branch", "base_try", "base_verdict", "base_ready", "facts", "task_brief"]
DESCRIPTION = (
    "Read the chosen Jira task, fetch source refs and prepare its isolated task branch. "
    "The task's existing sandbox is recreated: files and uncommitted work inside it are replaced. "
    "Committed task-branch work from the source checkout or origin is reused; the source working tree is preserved. "
    "A production merge conflict stops preparation for review. "
    "The result includes a Task Brief with paths and exact SHAs. "
    "Baseline tests, agent calls and implementation attempts are outside this action. "
    "Declared external writes: none (no Jira transition or push).")


def upgrade(document):
    result = copy.deepcopy(document)
    steps = {s["id"]: s for s in result.get("steps", [])}
    required = set(PREPARE_STEPS) - {"task_brief"}
    required.update(("budget", "baseline", "plan", "preflight", "implement", "base_resolve"))
    if required - set(steps):
        raise ValueError("Not the supported Claim graph; missing %s" % ", ".join(sorted(required - set(steps))))
    if steps["budget"].get("plugin") != "memory.remember" or steps["implement"].get("plugin") != "agent.implement":
        raise ValueError("Claim budget/implementation plugins changed; inspect the cycle before upgrading.")
    actions = result.setdefault("actions", {})
    if "prepare_task" in actions:
        if (actions["prepare_task"].get("steps") == PREPARE_STEPS and "task_brief" in steps
                and steps["workspace"].get("needs") == ["admit"]
                and steps["budget"].get("needs") == ["preflight", "baseline"]
                and steps["implement"].get("needs") == ["budget"]):
            return result
        raise ValueError("A different Prepare task action already exists.")
    if "task_brief" in steps:
        raise ValueError("The step id task_brief is already in use.")
    if steps["workspace"].get("needs") != ["budget"] or steps["budget"].get("needs") != ["admit"]:
        raise ValueError("The intake budget boundary changed; inspect it before upgrading.")
    steps["workspace"]["needs"] = ["admit"]
    command = steps["workspace"].get("with", {}).get("command", "")
    anchor = 'WD="$ROOT/$KEY"\n'
    if command.count(anchor) != 1 or 'cd "$WD"\n' not in command:
        raise ValueError("The workspace script changed; cannot capture recreation evidence safely.")
    command = command.replace(anchor, anchor + 'if [ -e "$WD" ]; then QAVECTOR_PREPARE_EXISTED=true; else QAVECTOR_PREPARE_EXISTED=false; fi\n', 1)
    command = command.replace('cd "$WD"\n', 'cd "$WD"\n' +
        "python3 - \"$QAVECTOR_PREPARE_EXISTED\" <<'PREPARE_JSON'\n" +
        "import json, sys\n" +
        "with open('.git/qavector-prepare.json', 'w') as handle:\n" +
        "    json.dump({'existed_before': sys.argv[1] == 'true'}, handle)\n" +
        "PREPARE_JSON\n", 1)
    steps["workspace"]["with"]["command"] = command
    steps["budget"]["needs"] = ["preflight", "baseline"]
    steps["budget"]["group"] = "Build"
    steps["budget"]["label"] = "Count this implementation attempt"
    steps["implement"]["needs"] = ["budget"]
    steps["base_ready"]["on_failure"] = "continue"
    for name in ("baseline", "plan"):
        if "task_brief" not in steps[name].get("needs", []):
            steps[name].setdefault("needs", []).append("task_brief")
    actions["prepare_task"] = {"title": "Prepare task", "description": DESCRIPTION,
                               "steps": list(PREPARE_STEPS), "result": "task_brief"}
    brief = {"id": "task_brief", "plugin": "task.brief", "label": "Prepared task and Git snapshots",
             "group": "Workspace", "needs": ["facts", "base_ready", "base_verdict"],
             "if": "${steps.base_verdict.outputs.stdout_tail} != ''",
             "with": {"issues": "${steps.todo.outputs.issues}",
                      "directory": "${vars.work_root}/${steps.todo.outputs.key}",
                      "source": "${vars.claim_dir}", "base": "origin/production-ecs",
                      "base_update": "${steps.base_verdict.outputs.stdout_tail}",
                      "task_report": "${steps.todo.outputs.html_path}"},
             "timeout": 120, "on_failure": "continue"}
    index = next(i for i, step in enumerate(result["steps"]) if step["id"] == "facts")
    result["steps"].insert(index + 1, brief)
    return result


def draft(source, transform=None):
    import yaml
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    cycle_id = os.path.splitext(os.path.basename(source))[0]
    raw = yaml.safe_load(text)
    updated = (transform or upgrade)(raw)
    cycle = model.parse_cycle(updated, cycle_id)
    problems = model.problems(cycle, registry, updated)
    if problems:
        raise ValueError("; ".join(problems))
    rendered = cyclepatch.patch(text, raw, updated, cyclefile._RENDERER)
    # Check the actual patched text too, not only the in-memory proposal.
    parsed = yaml.safe_load(rendered)
    problems = model.problems(model.parse_cycle(parsed, cycle_id), registry, parsed)
    if problems:
        raise ValueError("; ".join(problems))
    return text, rendered


def main(argv=None, transform=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    args = parser.parse_args(argv)
    if args.apply and not args.expected_digest:
        parser.error("--apply requires --expected-digest")
    if not args.apply and not args.output:
        parser.error("--output is required for a reviewable draft")
    try:
        before, after = draft(args.source, transform)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    digest = cyclefile.text_digest(before)
    if args.apply and digest != args.expected_digest:
        parser.error("source changed after review; create a new draft")
    destination = args.source if args.apply else args.output
    if not args.apply and os.path.realpath(destination) == os.path.realpath(args.source):
        parser.error("draft output must differ from source; use --apply after review")
    if args.apply:
        # Same atomic text writer as the cycle editor, preserving the source's
        # containing directory and leaving a complete YAML at every instant.
        with open(args.source, encoding="utf-8") as handle:
            if cyclefile.text_digest(handle.read()) != digest:
                parser.error("source changed during validation; create a new draft")
        descriptor, temporary = tempfile.mkstemp(prefix=".prepare-task-", dir=os.path.dirname(os.path.abspath(destination)))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(after)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(os.stat(args.source).st_mode))
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write(after)
    print("source_digest=%s\nwritten=%s" % (digest, destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
