"""The development cycle, end to end, against fakes.

Every other test in this suite proves one plugin. This one proves the **chain**
- that the steps are wired to each other the way the file says, that the gates
actually gate, and that the invariants hold across the whole of it. A cycle
whose plugins are each correct and whose wiring is wrong looks exactly like a
working one until the day it commits something nobody agreed to.

So this runs the real launcher, in a real subprocess, over the real
`cycles/development.yaml`, and the only things that are fake are the three
things that would cost money or reach somebody else's server:

- a Jira on a loopback port whose issue status really changes;
- a `claude` that writes real files and answers each review step differently,
  telling them apart by what their prompts ask for;
- a git repository in tmp_path, with a real commit in it.

Four questions, and none of them is "did it run":

1. Is the tree that was committed the tree that passed the checks? (INV-05)
2. Does a refusal at the gate leave Jira and the repository untouched?
   (INV-01, AC-03)
3. Does a second run of a finished task decline to do it again? (AC-16, INV-07)
4. Does an exhausted budget stop it, and say which limit it was?

It is slow - four launcher runs - and it is worth it. Nothing cheaper can tell
a wired-up cycle from a broken one.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: The base branch this harness builds, overridden into the cycle so no test
#: here depends on what the shipped template happens to call one - and so that
#: nothing in this repository names a branch somebody actually has.
BASE = "mainline"

#: To Do -> In Progress, named after the action the way a real workflow is.
WORKFLOW = {"To Do": [{"id": "11", "name": "Start Progress",
                       "to": {"name": "In Progress", "id": "s2"}}]}

#: The fake CLI. It is one binary doing two jobs, exactly as the real one is:
#: a review run gets the reading tools and no `--permission-mode`, an edit run
#: gets the other. The reviews are told apart by the sentence each step's own
#: prompt opens with - which also means this test fails loudly if somebody
#: rewrites a prompt without looking at what reads it.
CLAUDE = r'''#!%(python)s
import json, os, sys

if "auth" in sys.argv:
    print(json.dumps({"loggedIn": True, "email": "a@example.invalid",
                      "subscriptionType": "pro"}))
    sys.exit(0)

def say(one): print(json.dumps(one), flush=True)
say({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})

prompt = sys.argv[sys.argv.index("-p") + 1]

def answer(review):
    # The optional parts of a review, only when the prompt asked for them -
    # exactly what the plugin validates.
    if '"out_of_scope": [' in prompt:
        review = dict(review, out_of_scope=["A name that is only spaces is "
                                            "greeted as it is."])
    if '"complexity":' in prompt:
        review = dict(review, complexity="low")
    say({"type": "result", "subtype": "success", "is_error": False,
         "result": json.dumps(review), "total_cost_usd": 0.01})
    sys.exit(0)

if "--permission-mode" not in sys.argv:
    if "which of the things this task" in prompt:               # reconcile
        answer({"summary": "greet() does not handle an empty name.",
                "risk": "medium", "recommendations": [],
                "issues": [{"severity": "medium",
                            "description": "greet the nameless",
                            "file": "main.py", "line": 1}]})
    if "You did not write this code" in prompt and "diff --git" not in prompt:
        # A reviewer that was not shown the change cannot pass it.
        answer({"summary": "No diff was attached.", "risk": "high",
                "recommendations": [],
                "issues": [{"severity": "high",
                            "description": "the change was not shown",
                            "file": "", "line": None}]})
    if "You did not write this plan" in prompt:                 # business
        answer({"summary": "The plan matches the task.", "risk": "low",
                "issues": [], "recommendations": []})
    if "You did not write this code" in prompt:                 # code review
        answer({"summary": "The change is what was planned.", "risk": "low",
                "issues": [], "recommendations": []})
    if "Decide whether the task above is now done" in prompt:   # acceptance
        answer({"summary": "Every criterion is met.", "risk": "low",
                "issues": [], "recommendations": []})
    if "Write the plan" in prompt:                              # the plan
        answer({"summary": "Give greet() a polite default.", "risk": "low",
                "recommendations": ["main.py:1"],
                "issues": [{"severity": "low", "description": "empty name",
                            "file": "main.py", "line": 1}]})
    answer({"summary": "Looks right.", "risk": "low", "issues": [],
            "recommendations": []})       # implement's own review gate

if "try to break it" in prompt:                                 # probes
    # Written where the step runs, never into the checkout it may only read.
    # One passes and one fails, so the run shows both reach the review.
    here = os.getcwd()
    open(os.path.join(here, "test_probe.py"), "w").write(
        'from main import greet\n\n\n'
        'def test_no_name_at_all():\n    assert greet(None) == "Hello, there"\n\n\n'
        'def test_only_spaces():\n    assert greet("  ") == "Hello, there"\n')
    say({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": os.path.join(here, "test_probe.py")}}]}})
    say({"type": "result", "subtype": "success", "is_error": False,
         "result": "Two probes.", "total_cost_usd": 0.01})
    sys.exit(0)

where = sys.argv[sys.argv.index("--add-dir") + 1]
open(os.path.join(where, "main.py"), "w").write(
    'def greet(name):\n    return "Hello, " + (name or "there")\n')
open(os.path.join(where, "test_main.py"), "w").write(
    'from main import greet\n\n\n'
    'def test_a_name():\n    assert greet("Tester") == "Hello, Tester"\n\n\n'
    'def test_no_name():\n    assert greet("") == "Hello, there"\n')
say({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Write",
     "input": {"file_path": os.path.join(where, "main.py")}}]}})
say({"type": "result", "subtype": "success", "is_error": False,
     "result": "Wrote main.py and its test.", "total_cost_usd": 0.02})
'''


class _Jira(object):
    """A loopback Jira with one issue whose status actually changes."""

    def __init__(self, status="To Do"):
        self.status = status
        self.posts = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _send(self, code, payload):
                raw = b"" if code == 204 or payload is None else \
                    json.dumps(payload).encode()
                self.send_response(code)
                if raw:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def do_GET(self):                                  # noqa: N802
                if "/transitions" in self.path:
                    return self._send(200, {"transitions":
                                            WORKFLOW.get(outer.status, [])})
                if "/search" in self.path:
                    return self._send(200, {"total": 1, "issues": [{
                        "id": "1001", "key": "QA-7",
                        "fields": {"summary": "Greet the nameless",
                                   "status": {"name": outer.status},
                                   "assignee": {"displayName": "Tester"},
                                   "reporter": {"displayName": "Tester"},
                                   "created": "2026-09-01T10:00:00.000+0000",
                                   "updated": "2026-09-01T10:00:00.000+0000"}}]})
                if "/comment" in self.path:
                    return self._send(200, {"total": 1, "comments": [{
                        "author": {"displayName": "Tester"},
                        "created": "2026-09-01T11:00:00.000+0000",
                        "body": "It must work for an empty name too."}]})
                return self._send(200, {"key": "QA-7", "fields": {
                    "status": {"name": outer.status}}})

            def do_POST(self):                                 # noqa: N802
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)) or b"{}")
                if "/search" in self.path:
                    return self.do_GET()
                outer.posts.append(self.path)
                wanted = str((body.get("transition") or {}).get("id"))
                for one in WORKFLOW.get(outer.status, []):
                    if one["id"] == wanted:
                        outer.status = one["to"]["name"]
                        return self._send(204, None)
                self._send(400, {"errorMessages": ["not available"]})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.site = "http://127.0.0.1:%d" % self._server.server_port
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def transitions(self):
        return [one for one in self.posts if "/transitions" in one]

    def close(self):
        self._server.shutdown()
        self._server.server_close()


class _World(object):
    """A repository, a cycle pointed at it, and a launcher that can run it."""

    def __init__(self, root, jira, cycle="development"):
        self.root = str(root)
        self.jira = jira
        self.cycle = cycle
        self.repo = os.path.join(self.root, "repo")
        self.python = sys.executable

    def build(self, claude=None, **edits):
        """A clean repository and a fresh copy of the real cycle file.

        ``claude`` replaces the fake CLI's source, for a test that needs one of
        the review steps to answer differently. ``edits`` are replacements made
        in the cycle file, for a test that needs a different variable.
        """
        for name in ("repo", "cycles", "home", "remote.git"):
            shutil.rmtree(os.path.join(self.root, name), ignore_errors=True)
        os.makedirs(self.repo)
        _write(os.path.join(self.repo, "main.py"),
               'def greet(name):\n    return "Hello, " + name\n')
        _write(os.path.join(self.repo, "test_main.py"),
               'from main import greet\n\n\n'
               'def test_a_name():\n    assert greet("Tester") == "Hello, Tester"\n')
        # Every real checkout has one, and without it the checks leave
        # __pycache__ behind and the commit sweeps it in - which would make
        # this fixture prove something about a repository nobody has.
        _write(os.path.join(self.repo, ".gitignore"), "__pycache__/\n*.pyc\n")
        for argv in (["git", "init", "-q", "."], ["git", "add", "-A"],
                     ["git", "-c", "user.email=t@example.invalid",
                      "-c", "user.name=T", "commit", "-qm", "the subject"]):
            subprocess.run(argv, cwd=self.repo, check=True)

        if self.cycle == "development_in_progress":
            remote = os.path.join(self.root, "remote.git")
            for argv in (["git", "init", "--bare", "-q", remote],
                         ["git", "remote", "add", "origin", remote],
                         ["git", "push", "-q", "origin", "HEAD:" + BASE],
                         ["git", "config", "user.name", "Test"],
                         ["git", "config", "user.email", "test@example.invalid"]):
                subprocess.run(argv, cwd=self.repo, check=True)

        binary = os.path.join(self.root, "claude")
        _write(binary, (claude or CLAUDE) % {"python": self.python})
        os.chmod(binary, 0o755)

        os.makedirs(os.path.join(self.root, "cycles"))
        with open(os.path.join(ROOT, "cycles", self.cycle + ".yaml"),
                  encoding="utf-8") as handle:
            source = handle.read()
        # Only the fake CLI is patched into the text. Everything a variable
        # carries is overridden on the command line instead - see `run` - so
        # this harness does not care what the shipped template happens to say
        # today. It used to string-replace the template's own values, which
        # meant somebody editing the cycle in the application silently pointed
        # these tests at their real Jira.
        for old, new in edits.items():
            source = source.replace(old, new)
        # Inject by plugin, never by a particular model spelling. A template
        # switching from sonnet to opus must not escape the fake provider.
        import yaml
        document = yaml.safe_load(source)
        for step in document["steps"]:
            if step["plugin"] in ("agent.review", "agent.edit", "agent.implement"):
                step.setdefault("with", {})["claude"] = binary
        _write(os.path.join(self.root, "cycles", self.cycle + ".yaml"),
               yaml.safe_dump(document, sort_keys=False, allow_unicode=True))

    #: Everything that would otherwise reach the world outside this test,
    #: overridden where a cycle run is meant to be overridden. A variable named
    #: by a template this harness does not know about simply keeps its own
    #: value, which is the right answer rather than a failure.
    def overrides(self):
        return {"jira_site": self.jira.site,
                "jira_email": "test@example.invalid",
                "jira_projects": "",
                "jira_token": "sekret",
                "project_dir": self.repo,
                "git_base": BASE,
                "checks": "%s -m pytest -q" % self.python}

    def run(self, answer="Start work"):
        """One launcher run, answering the approval gate. Returns (code, steps)."""
        proc = subprocess.Popen(
            [self.python, os.path.join(ROOT, "session_launcher.py"),
             "--cycle-run=" + self.cycle,
             "--cycles-dir=" + os.path.join(self.root, "cycles")]
            + ["--cycle-var=%s=%s" % one for one in self.overrides().items()]
            + ["--events=-", "--control=-"],
            cwd=ROOT, env=dict(os.environ,
                               CMS_HOME=os.path.join(self.root, "home")),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        steps, asked = {}, []
        for line in proc.stdout:
            if not line.strip().startswith("{"):
                continue
            one = json.loads(line)
            if one.get("kind") == "cycle.ask":
                asked.append(one)
                proc.stdin.write(json.dumps(
                    {"command": "ask.result", "id": one["id"],
                     "answer": answer, "who": "tester"}) + "\n")
                proc.stdin.flush()
            elif one.get("kind") == "cycle.step.end":
                steps[one["step"]] = one
        proc.wait(timeout=600)
        return proc.returncode, steps, asked

    def remembered(self, key="dev/QA-7"):
        """What the store holds about one task. ``{}`` before any run wrote."""
        where = os.path.join(self.root, "home", "cyclememory.json")
        if not os.path.exists(where):
            return {}
        with open(where, encoding="utf-8") as handle:
            document = json.load(handle)
        return ((document.get("records") or {}).get(key) or {}).get("fields") or {}

    def commits(self):
        return subprocess.run(["git", "log", "--format=%H"], cwd=self.repo,
                              capture_output=True, text=True
                              ).stdout.strip().splitlines()

    def tree_of(self, sha):
        return subprocess.run(["git", "rev-parse", "%s^{tree}" % sha],
                              cwd=self.repo, capture_output=True,
                              text=True).stdout.strip()

    def message(self):
        return subprocess.run(["git", "log", "-1", "--format=%B"],
                              cwd=self.repo, capture_output=True,
                              text=True).stdout


def _write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _status(steps, name):
    return (steps.get(name) or {}).get("status", "never ran")


def _outputs(steps, name):
    return (steps.get(name) or {}).get("outputs") or {}


@pytest.fixture
def world(tmp_path):
    jira = _Jira()
    made = _World(tmp_path, jira)
    yield made
    jira.close()


# -------------------------------------------------------- a person approves
@pytest.fixture(scope="module")
def approved(tmp_path_factory):
    """One approved run and the re-run after it, shared by the tests below.

    Module-scoped because it is two launcher runs and several agent calls; the
    assertions are separate tests so a failure says which property broke.
    """
    jira = _Jira()
    made = _World(tmp_path_factory.mktemp("approved"), jira)
    try:
        made.build()
        first = made.run("Start work")
        again = made.run("Start work")
        yield made, first, again
    finally:
        jira.close()


def test_the_whole_chain_runs(approved):
    world, (code, steps, asked), _ = approved

    assert code == 0, [(k, v["status"], v.get("message")) for k, v in steps.items()]
    assert _status(steps, "commit") == "success"
    assert len(world.commits()) == 2, "the subject, and the change"


def test_what_was_committed_is_what_passed_the_checks(approved):
    """INV-05, and the reason `expect_tree` exists: the thing committed and the
    thing verified are one object rather than two descriptions of one."""
    world, (_code, steps, _asked), _ = approved

    verified = _outputs(steps, "implement").get("tree")
    committed = _outputs(steps, "commit").get("tree")

    assert verified and verified == committed
    assert committed == world.tree_of(_outputs(steps, "commit")["commit"])


def test_the_issue_moved_exactly_once(approved):
    world, _first, _again = approved

    assert world.jira.status == "In Progress"
    assert len(world.jira.transitions()) == 1


def test_the_commit_carries_the_trailer_a_rerun_recognises(approved):
    world, _first, _again = approved
    assert "QAVector-Operation:" in world.message()


def test_the_commit_subject_is_the_task_and_the_plan_is_its_body(approved):
    """The subject was once the plan's whole summary - a paragraph on one line,
    opening with whatever the plan said about itself."""
    world, _first, _again = approved
    subject, blank, body = world.message().partition("\n")
    assert subject == "QA-7: Greet the nameless"
    assert blank and body.strip(), "the plan is kept, in the body"


def test_it_commits_the_work_and_not_the_leavings(approved):
    """The checks run in the checkout and leave build artefacts behind. What
    is committed should be the change, not what running the tests produced."""
    world, _first, _again = approved
    changed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"], cwd=world.repo,
        capture_output=True, text=True).stdout.split()

    assert sorted(changed) == ["main.py", "test_main.py"], changed


def test_a_person_was_asked_before_anything_changed(approved):
    _world, (_code, _steps, asked), _ = approved

    assert len(asked) == 1
    assert "QA-7" in asked[0]["question"]


# ------------------------------------------------------ and again, tomorrow
def test_a_second_run_of_a_finished_task_declines_it(approved):
    """AC-16 and INV-07: a re-run does not redo finished work, and does not
    spend an agent call finding that out."""
    _world, _first, (code, steps, asked) = approved

    assert code != 0
    assert _status(steps, "admit") == "failed"
    assert _outputs(steps, "admit")["failed_check"] == "not already committed"
    assert _status(steps, "plan") == "skipped", "no agent was paid to re-plan"
    assert asked == [], "and nobody was interrupted about it"


def test_a_second_run_makes_no_second_commit(approved):
    world, _first, _again = approved
    assert len(world.commits()) == 2


# --------------------------------------------------------- a person refuses
def test_a_refusal_leaves_jira_and_the_repository_alone(world):
    """INV-01 and AC-03 in one run: no code is written and the board is not
    touched until somebody has agreed to the plan."""
    world.build()
    code, steps, asked = world.run("Leave it")

    assert asked, "it has to ask before it can be refused"
    assert _status(steps, "approve") == "failed"
    assert world.jira.status == "To Do"
    assert world.jira.transitions() == []
    assert _status(steps, "implement") == "skipped"
    assert len(world.commits()) == 1, "only the subject"
    assert code != 0, "a refused run is not a successful one"


def test_a_refused_run_still_writes_down_where_it_got_to(world):
    """The terminals the specification calls waiting and blocked: the next run
    starts from what this one recorded."""
    world.build()
    _code, steps, _asked = world.run("Leave it")

    assert _status(steps, "note") == "success"
    assert _status(steps, "report") == "success"


# ------------------------------------------- the checks and the reviewer disagree
#: The same fake, except that the independent reviewer finds something it will
#: not sign off. Everything before it is unchanged, so the work is really done
#: and the checks really pass - which is the whole point of the scenario.
REVIEWER_OBJECTS = CLAUDE.replace(
    '''    if "You did not write this code" in prompt:                 # code review
        answer({"summary": "The change is what was planned.", "risk": "low",
                "issues": [], "recommendations": []})''',
    '''    if "You did not write this code" in prompt:                 # code review
        answer({"summary": "It weakens a test.", "risk": "high",
                "issues": [{"severity": "high", "description": "a test was weakened",
                            "file": "test_main.py", "line": 1}],
                "recommendations": ["put it back"]})''')


def test_passing_checks_do_not_outvote_the_reviewer(world):
    """AC-07, and the reason there are two of them. The work built and its
    tests pass; a person reading the diff would still refuse it. "Verified" is
    not something one of the two gets to declare on its own."""
    assert REVIEWER_OBJECTS != CLAUDE, "the variant has to actually differ"
    world.build(claude=REVIEWER_OBJECTS)
    code, steps, _asked = world.run("Start work")

    assert _status(steps, "implement") == "success", "the work really was done"
    assert _status(steps, "review") == "success", "and the review really ran"
    assert _status(steps, "final") == "failed"
    assert _outputs(steps, "final")["failed_check"] == "the review is clean"
    assert _status(steps, "commit") == "skipped"
    assert len(world.commits()) == 1, "nothing was committed"
    assert code != 0


def test_a_refused_change_is_left_in_the_checkout_to_look_at(world):
    """The work is not thrown away because it was refused - somebody has to be
    able to read what the agent actually did."""
    world.build(claude=REVIEWER_OBJECTS)
    world.run("Start work")

    changed = subprocess.run(["git", "status", "--porcelain"], cwd=world.repo,
                             capture_output=True, text=True).stdout
    assert "main.py" in changed


# ------------------------------------------------------- it is already done
#: The same fake, except that the first agent finds the task already satisfied
#: by the committed code. Nothing after it should run.
ALREADY_DONE = CLAUDE.replace(
    '''        answer({"summary": "greet() does not handle an empty name.",
                "risk": "medium", "recommendations": [],
                "issues": [{"severity": "medium",
                            "description": "greet the nameless",
                            "file": "main.py", "line": 1}]})''',
    '''        answer({"summary": "greet() already handles the empty name, main.py:2.",
                "risk": "low", "issues": [], "recommendations": []})''')


def test_a_task_the_code_already_satisfies_is_not_done_again(world):
    """AC-11 and AC-12. The expensive half of the cycle exists to be skipped
    when the work turns out to have been done already - and skipping it must
    not look like a failure."""
    assert ALREADY_DONE != CLAUDE, "the variant has to actually differ"
    world.build(claude=ALREADY_DONE)
    _code, steps, asked = world.run("Start work")

    assert _status(steps, "reconcile") == "success"
    assert _status(steps, "work_remains") == "failed"
    assert _status(steps, "plan") == "skipped", "no plan was paid for"
    assert asked == [], "and nobody was interrupted"
    assert world.jira.status == "To Do", "nor was the board touched"
    assert len(world.commits()) == 1


def test_being_already_done_is_written_down_so_the_next_run_agrees(world):
    """Otherwise every run would pay an agent to rediscover it."""
    world.build(claude=ALREADY_DONE)
    world.run("Start work")
    _code, steps, _asked = world.run("Start work")

    assert _status(steps, "admit") == "failed"
    assert _outputs(steps, "admit")["failed_check"] == "not already committed"
    assert _status(steps, "reconcile") == "skipped", "not even asked again"


# ------------------------------------------------------------- the budget
def test_an_exhausted_budget_stops_it_and_names_the_limit(world):
    """A task that keeps failing must not be retried for ever, and the run has
    to say which limit it hit rather than only that it stopped."""
    world.build(**{"max_runs_per_task: 3": "max_runs_per_task: 0"})
    code, steps, asked = world.run("Start work")

    assert _status(steps, "admit") == "failed"
    assert _outputs(steps, "admit")["failed_check"] == "within its budget"
    assert world.jira.status == "To Do"
    assert asked == []
    assert code != 0


def test_a_run_the_gate_refused_does_not_spend_an_attempt(world):
    """Otherwise the budget measures how often somebody pressed Run. A task
    whose gate refuses - it is already committed, the checkout is dirty, the
    Jira query came back empty - climbed a counter nothing could bring back
    down, until a task with an unrelated problem could never be worked on
    again without emptying the store by hand."""
    world.build(**{"max_runs_per_task: 3": "max_runs_per_task: 0"})

    _code, steps, _asked = world.run("Start work")
    assert _status(steps, "admit") == "failed"
    assert _status(steps, "budget") == "skipped", "not counted, not run"
    assert world.remembered().get("attempts", 0) == 0

    _code, steps, _asked = world.run("Start work")
    assert world.remembered().get("attempts", 0) == 0, "still nothing spent"


def test_an_admitted_run_spends_one_attempt_before_it_works(world):
    """The other half of the same rule: the counter moves before any work of
    this run, so a run that dies mid-way has still spent an attempt."""
    world.build()

    _code, steps, _asked = world.run("Start work")

    assert _status(steps, "budget") == "success"
    assert world.remembered()["attempts"] == 1
    assert _status(steps, "reconcile") != "skipped", "the work followed it"


# ------------------------------------------------------------- the file itself
def test_the_cycle_as_shipped_is_valid():
    """Cheap, and it catches the thing every other test here would take a
    minute to discover: a step key or setting that refuses the run."""
    from cycle import loader, model, registry

    cycle, raw = loader.read_cycle("development",
                                   os.path.join(ROOT, "cycles"))
    assert model.problems(cycle, registry=registry, raw=raw) == []


def test_it_asks_before_it_writes_anything(approved):
    """Read off the graph rather than from a run: the approval gate must come
    before the Jira move and before the agent that edits."""
    from cycle import loader

    cycle, _raw = loader.read_cycle("development", os.path.join(ROOT, "cycles"))
    before = {step.id: set(step.needs) for step in cycle.steps}

    def reaches(start, target, seen=None):
        seen = seen if seen is not None else set()
        if start == target:
            return True
        return any(reaches(one, target, seen) for one in before.get(start, ())
                   if one not in seen and not seen.add(one))

    assert reaches("start", "approve"), "the Jira move waits on the approval"
    assert reaches("implement", "approve"), "so does the agent that edits"
    assert reaches("commit", "final"), "and the commit waits on the gate"


# ------------------------------------------- the variant that only reads Jira
# Same cycle, for work somebody has already picked up. The status is the
# instruction it is given rather than something for it to arrange - so the one
# thing that must be true of it is that it never writes to the board.
IN_PROGRESS = "development_in_progress"


def _variant():
    from cycle import loader

    cycle, _raw = loader.read_cycle(IN_PROGRESS, os.path.join(ROOT, "cycles"))
    return cycle


def test_the_in_progress_variant_is_valid():
    from cycle import model, registry

    cycle, raw = __import__("cycle.loader", fromlist=["x"]).read_cycle(
        IN_PROGRESS, os.path.join(ROOT, "cycles"))
    assert model.problems(cycle, registry=registry, raw=raw) == []


def test_it_never_writes_to_jira():
    """The whole difference from the ordinary cycle. A cycle that reads a board
    and then writes to it is one somebody has to trust with more than reading."""
    from cycle import registry

    for step in _variant().steps:
        plugin = registry.get(step.plugin)
        assert plugin is not None, step.plugin
        if step.plugin.startswith("jira."):
            assert step.plugin == "jira.issues", step.id
            assert plugin.metadata.permissions == ("network",)
            assert "reads only" in plugin.metadata.summary.lower()


def test_it_takes_only_work_that_is_already_in_progress():
    todo = _variant().step("todo")
    assert "status = " in todo.settings["jql"]
    assert "${vars.jira_in_progress}" in todo.settings["jql"]


def test_no_step_spells_the_status_out_for_itself():
    """It is named once, in a variable, and referenced everywhere else. A
    variable whose *value* mentioned another variable would not work - `${...}`
    makes a single pass - so the temptation is to write the literal twice, and
    two places saying one thing is how they come to disagree."""
    cycle = _variant()
    assert cycle.variables["jira_in_progress"] == "In Progress"

    for step in cycle.steps:
        for key, value in (step.settings or {}).items():
            assert "In Progress" not in str(value), (step.id, key)

    assert "${vars.jira_in_progress}" in cycle.step("todo").settings["jql"]


def test_it_still_asks_before_it_writes_any_code():
    """A task being In Progress says somebody meant to do it. It does not say
    they agreed to this plan."""
    cycle = _variant()
    assert cycle.step("approve") is not None
    assert cycle.step("approve").plugin == "approval.gate"

    before = {step.id: set(step.needs) for step in cycle.steps}

    def reaches(start, target, seen=None):
        seen = seen if seen is not None else set()
        if start == target:
            return True
        return any(reaches(one, target, seen) for one in before.get(start, ())
                   if one not in seen and not seen.add(one))

    assert reaches("implement", "approve")
    assert reaches("commit", "final")


def test_the_two_cycles_remember_a_task_under_the_same_key():
    """So whichever of them finishes one, the other will not pick it up again,
    and an attempt spent in one counts against the other's budget."""
    from cycle import loader

    ordinary, _ = loader.read_cycle("development", os.path.join(ROOT, "cycles"))
    variant = _variant()

    for name in ("history", "claim", "spent", "budget", "record", "note"):
        assert (ordinary.step(name).settings["key"]
                == variant.step(name).settings["key"]), name


def test_the_variant_keeps_shared_guards_and_adds_branch_and_plan_revision():
    from cycle import loader

    ordinary, _ = loader.read_cycle("development", os.path.join(ROOT, "cycles"))
    variant = _variant()

    assert {step.id for step in ordinary.steps} - {step.id
                                                   for step in variant.steps} \
        == {"start", "plan_is_sound"}
    assert {step.id for step in variant.steps} - {step.id
                                                  for step in ordinary.steps} \
        == {"prepare_branch", "settle", "recheck"}
    for name in ("history", "claim", "spent", "admit", "budget", "reconcile",
                 "work_remains", "final", "record", "record_reuse", "note", "report"):
        step = variant.step(name)
        assert step.settings == ordinary.step(step.id).settings, step.id
    assert "business" in variant.step("settle").needs
    assert "settle" in variant.step("recheck").needs
    assert "recheck" in variant.step("approve").needs


def test_task_branch_is_prepared_before_planning_and_required_at_commit():
    cycle = _variant()
    preparation = cycle.step("prepare_branch")
    assert preparation.plugin == "git.prepare_branch"
    assert preparation.settings["branch"] == "${steps.todo.outputs.key}"
    assert "prepare_branch" in cycle.step("reconcile").needs
    # What it is set to is the template's business and a test that pinned it
    # would fail the day somebody renamed their own base branch. That there is
    # one, and that the step reads it rather than a name of its own, is not.
    assert cycle.variables["git_base"]
    assert preparation.settings["base"] == "${vars.git_base}"
    assert preparation.settings["remote"] == "${vars.git_remote}"
    assert cycle.step("commit").settings["expect_branch"] == "${steps.todo.outputs.key}"


@pytest.fixture
def already_started(tmp_path):
    """The variant, against a board where the task is already In Progress."""
    jira = _Jira(status="In Progress")
    made = _World(tmp_path, jira, cycle=IN_PROGRESS)
    try:
        yield made
    finally:
        jira.close()


def test_the_variant_runs_the_whole_chain_and_commits(already_started):
    already_started.build()
    code, steps, asked = already_started.run("Start work")

    assert code == 0, [(k, v["status"], v.get("message"))
                       for k, v in steps.items()]
    assert _status(steps, "commit") == "success"
    assert _status(steps, "prepare_branch") == "success"
    assert _outputs(steps, "commit")["branch"] == "QA-7"
    assert subprocess.run(["git", "branch", "--show-current"], cwd=already_started.repo,
                          check=True, capture_output=True, text=True).stdout.strip() == "QA-7"
    assert len(already_started.commits()) == 2
    assert len(asked) == 1, "it still asks before it writes any code"


def test_every_agent_uses_the_fake_cli_regardless_of_model(already_started):
    import yaml

    already_started.build(**{"model: sonnet": "model: another-model"})
    path = os.path.join(already_started.root, "cycles", IN_PROGRESS + ".yaml")
    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    for step in document["steps"]:
        if step["plugin"].startswith("agent."):
            assert step["with"]["claude"] == os.path.join(already_started.root, "claude")


def test_the_variant_writes_nothing_to_jira(already_started):
    """The property it exists for. Proved by a board that would have recorded
    the write: the fake keeps every POST it is sent."""
    already_started.build()
    already_started.run("Start work")

    assert already_started.jira.posts == []
    assert already_started.jira.status == "In Progress", "exactly as it found it"


def test_the_variant_still_commits_the_tree_that_passed(already_started):
    already_started.build()
    _code, steps, _asked = already_started.run("Start work")

    verified = _outputs(steps, "implement").get("tree")
    committed = _outputs(steps, "commit").get("tree")
    assert verified and verified == committed
    assert committed == already_started.tree_of(
        _outputs(steps, "commit")["commit"])
    assert already_started.message().partition("\n")[0] == \
        "QA-7: Greet the nameless"


def test_a_refusal_in_the_variant_leaves_the_board_alone_too(already_started):
    already_started.build()
    _code, steps, asked = already_started.run("Leave it")

    assert asked, "it has to ask before it can be refused"
    assert _status(steps, "approve") == "failed"
    assert _status(steps, "implement") == "skipped"
    assert already_started.jira.posts == []
    assert already_started.jira.status == "In Progress"
    assert len(already_started.commits()) == 1


def test_the_shipped_templates_name_nobody_real():
    """These files are committed and shipped. A cycle edited in the
    application writes straight back into the one it was opened from, so a
    real instance and a work email land here without anybody deciding to put
    them there - and this is the only thing that would notice."""
    import re

    for name in ("development", "development_in_progress", "task_to_commit",
                 "todo_to_plan", "review_and_fix"):
        path = os.path.join(ROOT, "cycles", name + ".yaml")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            text = handle.read()

        for host in re.findall(r"https?://([\w.-]+)", text):
            assert (host.startswith("yourcompany.")
                    or host.endswith(".example")
                    or host.endswith(".invalid")
                    or host == "127.0.0.1"), "%s names %s" % (name, host)
        for address in re.findall(r"[\w.-]+@[\w.-]+", text):
            assert (address.endswith(".example")
                    or address.endswith(".invalid")), \
                "%s names %s" % (name, address)
        assert ROOT not in text, \
            "%s carries a path from the machine it was edited on" % name


def test_a_cycle_run_says_where_its_files_are(already_started):
    """The only way anything watching finds out. Without it a cycle's reports,
    its agents' transcripts and the JSON a Jira step saved were on disk and
    unreachable: History had no directory to remember and the Artifacts page
    had nothing to open."""
    already_started.build()
    where = []

    proc = subprocess.Popen(
        [already_started.python, os.path.join(ROOT, "session_launcher.py"),
         "--cycle-run=" + already_started.cycle,
         "--cycles-dir=" + os.path.join(already_started.root, "cycles")]
        + ["--cycle-var=%s=%s" % one
           for one in already_started.overrides().items()]
        + ["--events=-", "--control=-"],
        cwd=ROOT, env=dict(os.environ,
                           CMS_HOME=os.path.join(already_started.root, "home")),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1)
    started = []
    for line in proc.stdout:
        if not line.strip().startswith("{"):
            continue
        one = json.loads(line)
        if one.get("kind") == "run.dir":
            where.append(one.get("dir", ""))
        elif one.get("kind") == "cycle.run.start":
            started.append(one)
        elif one.get("kind") == "cycle.ask":
            proc.stdin.write(json.dumps(
                {"command": "ask.result", "id": one["id"],
                 "answer": "Leave it", "who": "t"}) + "\n")
            proc.stdin.flush()
    proc.wait(timeout=600)

    assert len(where) == 1, where
    assert os.path.isdir(where[0])
    assert os.path.exists(os.path.join(where[0], "metadata.json"))


def test_it_is_said_before_the_run_starts(already_started):
    """So a front-end has somewhere to put things from the first step, rather
    than finding out where they went once it is over."""
    already_started.build()
    order = []

    proc = subprocess.Popen(
        [already_started.python, os.path.join(ROOT, "session_launcher.py"),
         "--cycle-run=" + already_started.cycle,
         "--cycles-dir=" + os.path.join(already_started.root, "cycles"),
         "--cycle-only=todo"]
        + ["--cycle-var=%s=%s" % one
           for one in already_started.overrides().items()]
        + ["--events=-"],
        cwd=ROOT, env=dict(os.environ,
                           CMS_HOME=os.path.join(already_started.root, "home")),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    for line in proc.stdout:
        if line.strip().startswith("{"):
            kind = json.loads(line).get("kind")
            if kind in ("run.dir", "cycle.run.start"):
                order.append(kind)
    proc.wait(timeout=600)

    assert order[:2] == ["run.dir", "cycle.run.start"], order


# ------------------------------------------------ trying to break the change
def test_probes_are_written_outside_the_checkout_and_run_against_it(approved):
    """The probe tests are evidence, not part of the change: they must not be
    committed, and they must really run - the fake writes one that fails."""
    world, (_code, steps, _asked), _again = approved
    assert _status(steps, "probe") == "success"
    assert _outputs(steps, "probe")["files_changed"] == ["test_probe.py"]
    assert _outputs(steps, "probe_run")["exit_code"] == 1, "one probe fails"
    assert "test_only_spaces" in _outputs(steps, "probe_run")["stdout_tail"]
    committed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                               cwd=world.repo, capture_output=True,
                               text=True).stdout.split()
    assert "test_probe.py" not in committed
    assert not os.path.exists(os.path.join(world.repo, "test_probe.py"))


def test_a_failing_probe_does_not_stop_the_review_that_judges_it(approved):
    _world, (_code, steps, _asked), _again = approved
    assert _status(steps, "review") == "success"
    assert _status(steps, "commit") == "success"


def test_the_approval_shows_what_the_plan_left_out(approved):
    _world, (_code, _steps, asked), _again = approved
    detail = json.dumps(asked)
    assert "Left out of scope" in detail
    assert "A name that is only spaces" in detail


def test_the_code_review_is_shown_the_change_itself(approved):
    """It cannot run git, so the diff is attached. The fake reviewer blocks
    the commit when it is missing."""
    _world, (_code, steps, _asked), _again = approved
    said = _outputs(steps, "diff")["stdout_tail"]
    assert "diff --git a/main.py" in said
    assert "diff --git a/test_main.py" in said
    assert _status(steps, "commit") == "success"


def test_the_commit_body_is_the_plan_without_remarks_about_it(already_started):
    already_started.build()
    already_started.run("Start work")
    body = already_started.message().partition("\n\n")[2]
    assert body.strip() and not body.startswith(("Unchanged", "Revised"))


def test_the_attached_diff_includes_files_git_does_not_track_yet(tmp_path):
    """`git diff HEAD` alone leaves out a file the change created."""
    import yaml
    with open(os.path.join(ROOT, "cycles", "development_in_progress.yaml"),
              encoding="utf-8") as handle:
        command = [one for one in yaml.safe_load(handle)["steps"]
                   if one["id"] == "diff"][0]["with"]["command"]
    repo = str(tmp_path)
    for argv in (["git", "init", "-q"], ["git", "add", "."],
                 ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T",
                  "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(argv, cwd=repo, check=True)
    with open(os.path.join(repo, "brand_new.py"), "w") as handle:
        handle.write("x = 1\n")
    said = subprocess.run(command, shell=True, cwd=repo, capture_output=True,
                          text=True).stdout
    assert "brand_new.py" in said and "new file mode" in said
    assert subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True,
                          text=True).stdout.strip() == "?? brand_new.py", \
        "showing the change must not stage it"
