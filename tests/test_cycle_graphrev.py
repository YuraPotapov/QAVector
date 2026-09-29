"""The rules a run's graph would have to keep to, to grow during the run.

See docs/cycle-adaptive-design.md. Nothing here runs a cycle: the executor does
not call cycle/graphrev.py yet. What is tested is that every rule the design
states is one the code actually enforces - a proposal that breaks one is
refused with that reason, and the graph is left exactly as it was.
"""

import copy

from cycle import graphrev
from cycle.registry import CyclePlugin, PluginMetadata, output


class _Registry:
    """The plugins the example uses, and the point's own, as metadata only."""

    def __init__(self):
        self._plugins = {}
        for plugin_id, outputs in (("command.shell", ("exit_code", "stdout")),
                                   ("agent.review", ("summary", "proposal")),
                                   ("agent.implement", ("verified", "tree")),
                                   ("git.commit", ("commit",)),
                                   ("report.html", ()),
                                   ("plan.revise", ("revision",))):
            plugin = CyclePlugin()
            plugin.metadata = PluginMetadata(id=plugin_id, name=plugin_id,
                                             outputs=tuple(output(key) for key in outputs))
            plugin.problems = lambda settings: []
            self._plugins[plugin_id] = plugin

    def get(self, plugin_id):
        return self._plugins.get(plugin_id)

    def all_plugins(self):
        return list(self._plugins.values())


REGISTRY = _Registry()

TEMPLATE = {
    "id": "adaptive", "variables": {"token": {"secret": True}, "dir": "/src"},
    "steps": [
        {"id": "check", "plugin": "command.shell", "with": {"command": "pytest"}},
        {"id": "propose_fix", "plugin": "agent.review", "needs": ["check"]},
        {"id": "correct", "plugin": "plan.revise", "needs": ["propose_fix"],
         "with": {"proposer": "propose_fix", "allow": ["agent.implement"],
                  "attach_after": ["check"], "before": ["summary"],
                  "max_steps": 2, "max_revisions": 2}},
        {"id": "summary", "plugin": "report.html", "needs": ["check", "correct"],
         "if": "${run.id}"},
    ]}


def fix(step_id="fix", **extra):
    step = {"id": step_id, "plugin": "agent.implement", "needs": ["correct"],
            "with": {"task": "guard it", "directory": "${vars.dir}"}}
    step.update(extra)
    return step


def proposal(*add, base=0, **extra):
    made = {"schema": graphrev.SCHEMA, "base_revision": base, "point": "correct",
            "add": list(add or [fix()])}
    made.update(extra)
    return made


def check(made, revisions=(), statuses=None):
    return graphrev.validate(TEMPLATE, list(revisions), made, statuses or {},
                             REGISTRY)


# ------------------------------------------------------------------ accepted
def test_a_proposal_within_the_point_s_rules_is_accepted():
    problems, after = check(proposal())
    assert problems == []
    ids = [one["id"] for one in after["steps"]]
    assert ids == ["check", "propose_fix", "correct", "fix", "summary"]


def test_what_the_point_says_comes_before_waits_for_what_was_added():
    _problems, after = check(proposal())
    summary = next(one for one in after["steps"] if one["id"] == "summary")
    assert summary["needs"] == ["check", "correct", "fix"]


def test_a_recorded_revision_replays_to_the_same_graph():
    revision = graphrev.record(TEMPLATE, [], proposal(), {"by": "person"}, REGISTRY)
    _problems, after = check(proposal())
    assert graphrev.effective(TEMPLATE, [revision]) == after
    assert revision["revision"] == 1 and revision["add"][0]["id"] == "fix"
    assert revision["definitions"]["fix"], "the added step is fingerprinted"


def test_the_same_proposal_is_recognised_as_already_applied():
    """A crash between the revision and the point's own receipt runs the
    point again; it must find its work done rather than add it twice."""
    revision = graphrev.record(TEMPLATE, [], proposal(), {}, REGISTRY)
    assert graphrev.applied([revision], proposal()) is revision
    assert graphrev.applied([revision], proposal(fix("other"))) is None


# ------------------------------------------------------------------ refused
def test_a_stale_proposal_is_refused():
    revision = graphrev.record(TEMPLATE, [], proposal(), {}, REGISTRY)
    problems, after = check(proposal(fix("second")), [revision])
    assert any("stale" in one for one in problems)
    assert after == graphrev.effective(TEMPLATE, [revision]), "nothing moved"


def test_a_plugin_the_point_does_not_allow_is_refused():
    problems, _after = check(proposal(fix(plugin="git.commit")))
    assert any("git.commit is not a plugin correct allows" in one for one in problems)


def test_a_step_that_does_not_wait_for_the_point_is_refused():
    problems, _after = check(proposal(fix(needs=["check"])))
    assert any("does not wait for correct" in one for one in problems)


def test_a_step_may_not_wait_on_what_the_point_did_not_name():
    problems, _after = check(proposal(fix(needs=["correct", "propose_fix"])))
    assert any("may wait only for" in one and "propose_fix" in one for one in problems)


def test_a_taken_id_is_refused_including_one_removed_earlier():
    problems, _after = check(proposal(fix("check")))
    assert any("check: that id is taken" in one for one in problems)

    first = graphrev.record(TEMPLATE, [], proposal(fix("tmp")), {}, REGISTRY)
    second = {"revision": 2, "base": 1, "point": "correct", "remove": ["tmp"],
              "add": [], "replace": [], "before": []}
    problems, _after = check(proposal(fix("tmp"), base=2), [first, second])
    assert any("removed" in one for one in problems)


def test_what_has_started_is_never_rewritten():
    first = graphrev.record(TEMPLATE, [], proposal(), {}, REGISTRY)
    later = proposal(base=1, add=[], remove=["fix"])
    problems, _after = check(later, [first], statuses={"fix": "running"})
    assert any("already started" in one for one in problems)
    problems, _after = check(later, [first], statuses={"fix": "pending"})
    assert problems == []


def test_only_what_the_point_added_may_be_removed():
    problems, _after = check(proposal(add=[], remove=["check"]))
    assert any("only steps correct itself added" in one for one in problems)


def test_a_secret_is_not_read_unless_the_point_allows_it():
    step = fix(**{"with": {"task": "use ${vars.token}"}})
    problems, _after = check(proposal(step))
    assert any("reads the secret 'token'" in one for one in problems)


def test_an_output_nobody_produces_is_refused():
    step = fix(**{"with": {"task": "${steps.check.outputs.nonsense}"}})
    problems, _after = check(proposal(step))
    assert any("check does not produce" in one for one in problems)


def test_the_point_s_limits_hold():
    many = [fix("a"), fix("b"), fix("c")]
    problems, _after = check(proposal(*many))
    assert any("allows 2 at a time" in one for one in problems)

    first = graphrev.record(TEMPLATE, [], proposal(fix("a")), {}, REGISTRY)
    second = graphrev.record(TEMPLATE, [first], proposal(fix("b"), base=1), {}, REGISTRY)
    problems, _after = check(proposal(fix("c"), base=2), [first, second])
    assert any("max_revisions" in one for one in problems)


def test_what_the_whole_cycle_says_is_checked_too():
    """A loop through new steps, or a condition reading a step it does not
    wait for, is what model.problems already finds - and finds here."""
    looped = [fix("x", needs=["correct", "y"]), fix("y", needs=["x"])]
    problems, _after = check(proposal(*looped))
    assert any("wait for each other in a loop" in one for one in problems)


def test_a_policy_decided_point_may_only_add_what_changes_nothing():
    template = copy.deepcopy(TEMPLATE)
    template["steps"][2]["with"]["approve"] = "policy"
    problems, _after = graphrev.validate(template, [], proposal(), {}, REGISTRY)
    assert any("approve: policy" in one and "agent.implement" in one for one in problems)


def test_recording_what_validation_refused_raises():
    import pytest
    with pytest.raises(ValueError):
        graphrev.record(TEMPLATE, [], proposal(fix(plugin="git.commit")), {}, REGISTRY)


def test_the_template_is_never_changed_by_checking_or_replaying():
    before = copy.deepcopy(TEMPLATE)
    check(proposal())
    graphrev.effective(TEMPLATE, [graphrev.record(TEMPLATE, [], proposal(), {}, REGISTRY)])
    assert TEMPLATE == before


# ------------------------------------------------------------- partial runs
# Decided 2026-09-28: Run this step (>) and Run from here (>>) work on any step
# of the run on screen - one a revision added included.
def grown():
    return [graphrev.record(TEMPLATE, [], proposal(), {"by": "person"}, REGISTRY)]


def test_a_step_a_revision_added_can_be_run_on_its_own():
    answer = graphrev.partial(TEMPLATE, grown(), "fix", "only", source_run="r1")
    assert answer["problems"] == []
    assert answer["chosen"] == ["fix"]
    assert "fix" in [one["id"] for one in answer["document"]["steps"]]
    assert [one["source_run"] for one in answer["revisions"]] == ["r1"]


def test_running_from_an_added_step_runs_what_waits_for_it():
    answer = graphrev.partial(TEMPLATE, grown(), "fix", "from", source_run="r1")
    assert answer["chosen"] == ["fix", "summary"]


def test_the_inherited_revisions_replay_to_the_graph_the_run_had():
    revisions = grown()
    answer = graphrev.partial(TEMPLATE, revisions, "summary", "only", "r1")
    assert graphrev.effective(TEMPLATE, answer["revisions"]) == \
        graphrev.effective(TEMPLATE, revisions)


def test_a_point_run_again_decides_its_changes_again():
    """Its old additions are not inherited, so nothing of them is borrowed,
    and what it proposes now is checked against the graph without them."""
    answer = graphrev.partial(TEMPLATE, grown(), "correct", "only", "r1")
    assert answer["revisions"] == []
    assert "fix" not in [one["id"] for one in answer["document"]["steps"]]
    assert answer["chosen"] == ["correct"]
    problems, _after = graphrev.validate(answer["document"], answer["revisions"],
                                         proposal(), {}, REGISTRY)
    assert problems == [], "a fresh proposal fits the graph the partial run has"


def test_running_from_a_point_runs_it_and_what_comes_after_without_its_old_steps():
    answer = graphrev.partial(TEMPLATE, grown(), "correct", "from", "r1")
    assert answer["chosen"] == ["correct", "summary"]


def test_a_step_that_is_not_in_the_run_is_refused():
    answer = graphrev.partial(TEMPLATE, grown(), "ghost", "only")
    assert answer["problems"] == ["ghost is not a step of this run."]
