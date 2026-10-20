"""Review/apply six execution phases to the already prepared Claim workflow.

Uses prepare_claim's comment-preserving, digest-checked atomic writer. This
changes phase membership/policies, never commands, step ids or task counters.
"""

import copy

from cycle import prepare_claim

MEMBERS = {
    "prepare": "queue pick todo claim spent admit workspace branch base_try base_verdict base_resolve base_ready facts task_brief".split(),
    "plan": "baseline plan business settle recheck approve preflight".split(),
    "implement": "budget start implement own_tests full_tests diff review accept tests_verdict explain_failure final".split(),
    "integrate": "commit bring_back record dev_try dev_verdict dev_branch dev_resolve dev_merge dev_ready dev_own_tests dev_full_tests dev_bring_back scenarios_known scenario_draft write_scenario install_scenario scenario_ids local_server local_config local_ready local_scenarios local_stop local_ok local_explain".split(),
    "publish": "push_plan push_ok push record_dev".split(),
    "verify": "deployed dev_config dev_scenarios dev_forget_config dev_ok dev_explain ready note report".split(),
}

DEFINITIONS = [
    {"id": "prepare", "title": "Prepare task", "policy": "stop_after", "outcome": "task_prepared",
     "purpose": "Read task facts and prepare the sandbox. Full Run retains the configured conflict-resolution agent; the Prepare task action makes no agent calls.", "deliverables": ["task_brief"]},
    {"id": "plan", "title": "Analyse and plan", "policy": "stop_after", "outcome": "plan_ready",
     "purpose": "Run baseline checks, review the plan and obtain its existing approval before implementation.", "deliverables": ["baseline", "settle", "recheck", "approve", "preflight"]},
    {"id": "implement", "title": "Implement and verify", "policy": "stop_after", "outcome": "implementation_verified",
     "purpose": "Charge the implementation attempt, update Jira and implement and check the change in the sandbox.", "deliverables": ["implement", "own_tests", "full_tests", "review", "final"]},
    {"id": "integrate", "title": "Integrate locally", "policy": "stop_after", "outcome": "integrated_locally",
     "purpose": "Commit verified work, integrate it locally and run local acceptance before publishing.", "deliverables": ["commit", "dev_own_tests", "dev_full_tests", "local_scenarios", "local_ok"]},
    {"id": "publish", "title": "Publish to development", "policy": "stop_after", "outcome": "published_to_dev",
     "purpose": "Review the existing push plan and approval, then publish the configured refs.", "deliverables": ["push_plan", "push_ok", "push"]},
    {"id": "verify", "title": "Verify and hand over", "policy": "automatic",
     "purpose": "Confirm deployment, run development acceptance and finish the existing hand-over workflow.", "deliverables": ["dev_scenarios", "dev_ok", "ready", "report"]},
]


def upgrade(document):
    result = copy.deepcopy(document)
    assignments = {name: phase for phase, names in MEMBERS.items() for name in names}
    if set(assignments) != {step.get("id") for step in result.get("steps", [])}:
        raise ValueError("Unknown Claim graph: phase migration requires the reviewed 69-step Prepare workflow.")
    if "prepare_task" not in result.get("actions", {}):
        raise ValueError("Apply the Prepare task migration first.")
    if result.get("phases") and result["phases"] != DEFINITIONS:
        raise ValueError("Existing phase policies differ; review them instead of overwriting them.")
    result["phases"] = copy.deepcopy(DEFINITIONS)
    for step in result["steps"]:
        step["phase"] = assignments[step["id"]]
    return result


def main(argv=None):
    return prepare_claim.main(argv, transform=upgrade)


if __name__ == "__main__":
    raise SystemExit(main())
