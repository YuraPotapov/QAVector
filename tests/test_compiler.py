import os
import textwrap

import pytest

from domain.plan import GROUP, STEP
from engine import compiler, loader
from engine.context import ParamError, RunContext

# The engine's OWN fixture flows - see tests/test_loader.py for why.
FLOWS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "flows")


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _write(base, relpath, content):
    path = os.path.join(str(base), relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(content))


# --- browser_actions: what --no-browser may run ------------------------------
def test_a_scenario_of_host_checks_needs_no_browser():
    # demo_simple is `use: common.host_up` - an HTTP probe, reached through a
    # block, which is exactly where a page step would hide.
    assert compiler.browser_actions("demo_simple", FLOWS) == []


def test_a_scenario_that_drives_a_page_names_what_it_does():
    actions = compiler.browser_actions("demo_smoke", FLOWS)
    assert "assert_exists" in actions           # its own step, not only its blocks'
    assert actions == sorted(actions)
    assert not set(actions) & compiler.NO_BROWSER


def test_service_steps_and_their_checks_need_no_browser(tmp_path):
    _write(tmp_path, "scenarios/restart.yaml", """
        id: restart
        name: Restart
        tags: []
        steps:
          - service_restart: "Storefront/Web"
          - wait_for_out: {target: "Storefront/Web", value: "running on .+:8069"}
          - wait_for_service: "Storefront/Web"
          - wait_for_criterion: {target: "Storefront/Web", value: "Started"}
          - assert_host_up: "{{env.origin}}"
    """)
    # No run context either: a placeholder does not decide whether a page is needed.
    assert compiler.browser_actions("restart", str(tmp_path)) == []


def test_one_page_step_in_a_block_is_enough_to_need_a_browser(tmp_path):
    _write(tmp_path, "nav/open.yaml", """
        id: nav.open
        name: Open
        steps:
          - goto: "{{env.origin}}"
    """)
    _write(tmp_path, "scenarios/s.yaml", """
        id: s
        name: S
        tags: []
        steps:
          - service_start: "Storefront/Web"
          - use: nav.open
    """)
    assert compiler.browser_actions("s", str(tmp_path)) == ["goto"]


def test_browser_actions_still_refuses_a_cycle(tmp_path):
    _write(tmp_path, "loop/one.yaml", """
        id: loop.one
        name: One
        steps:
          - use: loop.two
    """)
    _write(tmp_path, "loop/two.yaml", """
        id: loop.two
        name: Two
        steps:
          - use: loop.one
    """)
    _write(tmp_path, "scenarios/s.yaml", """
        id: s
        name: S
        tags: []
        steps:
          - use: loop.one
    """)
    with pytest.raises(compiler.CompileError, match="cyclic"):
        compiler.browser_actions("s", str(tmp_path))


# --- parse_step: both YAML shapes -------------------------------------------
def test_parse_shorthand_use():
    step = compiler.parse_step({"use": "auth.login"})
    assert step.action == "use" and step.target == "auth.login"


def test_parse_shorthand_selector():
    step = compiler.parse_step({"click": "submit_button"})
    assert step.action == "click" and step.target == "submit_button"


def test_parse_shorthand_selector_and_value():
    step = compiler.parse_step({"fill": {"target": "login_input", "value": "admin"}})
    assert step.target == "login_input" and step.value == "admin"


def test_parse_verbose_form():
    step = compiler.parse_step({"type": "assert_url_contains", "value": "/web"})
    assert step.action == "assert_url_contains" and step.value == "/web"


def test_parse_assert_host_up_is_value_only():
    step = compiler.parse_step({"assert_host_up": "http://localhost:8069"})
    assert step.action == "assert_host_up" and step.value == "http://localhost:8069"


def test_parse_unknown_action_raises():
    with pytest.raises(compiler.CompileError):
        compiler.parse_step({"frobnicate": "x"})


def test_parse_fill_needs_mapping():
    with pytest.raises(compiler.CompileError):
        compiler.parse_step({"fill": "not-a-mapping"})


def test_parse_shorthand_must_be_single_key():
    with pytest.raises(compiler.CompileError):
        compiler.parse_step({"click": "a", "fill": "b"})


# --- service steps ----------------------------------------------------------
def test_parse_shorthand_service_reference():
    step = compiler.parse_step({"service_restart": "Storefront/Web"})
    assert step.action == "service_restart" and step.target == "Storefront/Web"


def test_parse_service_wait_takes_target_and_value():
    step = compiler.parse_step({"wait_for_out": {"target": "Storefront/Web",
                                                 "value": ".+:8069",
                                                 "timeout": 120000}})
    assert (step.target, step.value, step.timeout) == ("Storefront/Web", ".+:8069", 120000)


def test_parse_service_wait_verbose_form():
    step = compiler.parse_step({"type": "wait_for_criterion",
                                "target": "Storefront/Web", "value": "start"})
    assert step.action == "wait_for_criterion" and step.value == "start"


def test_service_step_needs_a_reference():
    with pytest.raises(compiler.CompileError):
        compiler.parse_step({"type": "service_start"})


def test_service_wait_needs_both_halves():
    with pytest.raises(compiler.CompileError):
        compiler.parse_step({"wait_for_out": {"target": "Storefront/Web"}})
    with pytest.raises(compiler.CompileError):
        compiler.parse_step({"wait_for_criterion": {"value": "start"}})


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_service_wait_with_a_blank_value_is_refused(blank):
    """An empty value box is an error, not a wait that always passes.

    The scenario editor drops a blank cell rather than writing it, and the YAML
    writer renders a missing value as ``value: ""`` - so this is the shape a
    half-filled step arrives in, not a hypothetical. For wait_for_out it is the
    dangerous one: an empty regex matches the first line the service prints, so
    the step would pass instantly having proved nothing.
    """
    for action in ("wait_for_out", "wait_for_criterion"):
        with pytest.raises(compiler.CompileError) as excinfo:
            compiler.parse_step({action: {"target": "Storefront/Web", "value": blank}})
        assert action in str(excinfo.value)


def test_a_service_wait_with_a_blank_target_is_refused():
    with pytest.raises(compiler.CompileError):
        compiler.parse_step({"wait_for_out": {"target": "  ", "value": "ready"}})


def test_a_service_reference_is_not_looked_up_as_a_selector():
    # It is a Project/Service pair, so a selectors.yaml entry that happens to
    # share its name must not stand in for it.
    step = compiler.parse_step({"service_start": "dashboard"})
    compiler._finalize(step, {"dashboard": ".app-root"}, None)
    assert step.target == "dashboard"


def test_wait_for_out_rejects_a_regex_that_will_not_compile():
    step = compiler.parse_step({"wait_for_out": {"target": "Storefront/Web",
                                                 "value": "[unclosed"}})
    with pytest.raises(compiler.CompileError) as excinfo:
        compiler._finalize(step, {}, None)
    assert "does not compile" in str(excinfo.value)


def test_wait_for_out_compiles_a_pattern_built_from_a_param():
    # The check has to run AFTER substitution, or a valid pattern with a
    # placeholder in it would be judged on the placeholder.
    ctx = RunContext(env={"origin": "http://localhost:8069"})
    step = compiler.parse_step({"wait_for_out": {"target": "Storefront/Web",
                                                 "value": "{{env.origin}}"}})
    compiler._finalize(step, {}, ctx)
    assert step.value == "http://localhost:8069"


# --- compile_scenario: expansion, selectors, params, cycles -----------------
def test_compile_expands_use_blocks():
    selectors = loader.load_selectors(FLOWS)
    ctx = RunContext(env={"origin": "http://localhost:8069"})
    steps = compiler.compile_scenario("demo_smoke", FLOWS, selectors, ctx)
    actions = [s.action for s in steps]
    assert "use" not in actions            # all blocks expanded away
    assert actions[0] == "assert_host_up"  # first test: host answered (via auth.login)
    assert "assert_visible" in actions     # dashboard readiness gate
    assert "click" in actions              # from demo.open_menu


def test_compile_resolves_named_selectors():
    selectors = loader.load_selectors(FLOWS)
    ctx = RunContext(env={"origin": "http://localhost:8069"})
    steps = compiler.compile_scenario("demo_smoke", FLOWS, selectors, ctx)
    targets = [s.target for s in steps]
    assert ".app-root" in targets       # dashboard -> css


def test_compile_substitutes_params(tmp_path):
    _write(tmp_path, "nav/go.yaml", """
        id: nav.go
        steps:
          - goto: "{{env.origin}}/web"
    """)
    _write(tmp_path, "scenarios/s.yaml", """
        id: s
        steps:
          - use: nav.go
    """)
    ctx = RunContext(env={"origin": "http://localhost:8069"})
    steps = compiler.compile_scenario("s", str(tmp_path), {}, ctx)
    assert steps[0].target == "http://localhost:8069/web"


# The single test that compiles the REAL flows tree, if one is present. Everything
# else above runs against tests/fixtures/flows, so the compiler stays testable
# when the app's scenarios move to their own repo (--flows-dir).
REAL_FLOWS = loader.DEFAULT_FLOWS_DIR


@pytest.mark.skipif(not os.path.isdir(os.path.join(REAL_FLOWS, "scenarios")),
                    reason="no flows/ tree here - they live in their own repo")
def test_every_real_scenario_compiles():
    """Catch a broken selector name or a missing block across the whole tree."""
    selectors = loader.load_selectors(REAL_FLOWS)
    ctx = RunContext(user={"login": "u", "class": "C"},
                     env={"origin": "http://localhost:8069", "url": "http://localhost:8069"})
    ids = loader.discover_scenarios(REAL_FLOWS, include_templates=True)
    assert ids, "a flows/ tree exists but contains no scenarios"
    for scenario_id in ids:
        steps, _root = compiler.compile_plan(scenario_id, REAL_FLOWS, selectors, ctx)
        assert steps, "%s compiled to no steps" % scenario_id
        assert "use" not in [s.action for s in steps], "%s left a use: unexpanded" % scenario_id


# --- compile_plan: the execution tree (for the overlay) ---------------------
def test_compile_plan_leaves_match_flat_steps():
    selectors = loader.load_selectors(FLOWS)
    ctx = RunContext(env={"origin": "http://localhost:8069"})
    steps, root = compiler.compile_plan("demo_smoke", FLOWS, selectors, ctx)
    flat = compiler.compile_scenario("demo_smoke", FLOWS, selectors, ctx)
    # compile_plan's flat list is identical to compile_scenario's
    assert [s.action for s in steps] == [s.action for s in flat]
    assert [s.target for s in steps] == [s.target for s in flat]
    # leaf order + indices line up 1:1 with the flat step list (== runner order)
    leaves = list(root.leaves())
    assert [leaf.step_index for leaf in leaves] == list(range(len(steps)))
    assert [leaf.action for leaf in leaves] == [s.action for s in steps]


def test_compile_plan_builds_group_hierarchy_with_stable_ids():
    selectors = loader.load_selectors(FLOWS)
    ctx = RunContext(env={"origin": "http://localhost:8069"})
    _steps, root = compiler.compile_plan("demo_smoke", FLOWS, selectors, ctx)
    assert root.kind == GROUP and root.id == "0" and root.label
    ids = [n.id for n in _walk(root)]
    assert len(ids) == len(set(ids))              # ids are unique
    # every use:-d block became a nested group (auth.login, demo.open_menu, ...)
    groups = [n for n in _walk(root) if n.kind == GROUP]
    assert len(groups) >= 3
    # leaf labels use the friendly (named) target, not the resolved CSS
    assert any("apps_button" in leaf.label for leaf in root.leaves())


def test_compile_plan_leaf_targets_are_resolved_css():
    selectors = loader.load_selectors(FLOWS)
    ctx = RunContext(env={"origin": "http://localhost:8069"})
    steps, _root = compiler.compile_plan("demo_smoke", FLOWS, selectors, ctx)
    # the flat steps still carry resolved CSS (dashboard -> .app-root)
    assert ".app-root" in [s.target for s in steps]


def test_compile_detects_cycles(tmp_path):
    _write(tmp_path, "a.yaml", "id: a\nsteps:\n  - use: b\n")
    _write(tmp_path, "b.yaml", "id: b\nsteps:\n  - use: a\n")
    with pytest.raises(compiler.CompileError):
        compiler.compile_scenario("a", str(tmp_path), {}, RunContext())


def test_compile_unknown_param_raises(tmp_path):
    _write(tmp_path, "scenarios/s.yaml", 'id: s\nsteps:\n  - goto: "{{env.missing}}"\n')
    with pytest.raises(ParamError):
        compiler.compile_scenario("s", str(tmp_path), {}, RunContext(env={}))
