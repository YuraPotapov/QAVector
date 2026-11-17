"""What agent work used and cost, summed one way for every view (QCR ADR-008).

* Thinking is part of output and never added again; the total is input +
  cache read + cache write + output.
* A cost says what it is: the program's estimate, or unknown - and unknown is
  counted as such, never as 0. Old turns read the same way new ones are written.
* A conversation's own figures and those inclusive of what it handed on to
  other agents are kept apart; an issue's ref includes its delegated work.
* A cycle step's conversation is summed by cycle, run and step from its ref;
  one recorded before refs, by its label.
"""

import json
import os
import sys

from integrations.agents import sessions, usage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def turn(**more):
    return dict({"n": 1, "started": 1000.0, "ended": 1010.0, "state": "completed", "model": "sonnet",
                 "tokens": {"input": 10, "cache_read": 100, "cache_write": 5, "output": 20, "thinking": 8},
                 "cost_usd": 0.5, "cost_kind": "estimate"}, **more)


def session(id_, *turns, connection="claude", source=None, model=""):
    return {"id": id_, "connection": connection, "connection_name": connection.capitalize(), "model": model,
            "source": source or {"label": "Chat"}, "turns": list(turns)}


def test_thinking_is_part_of_output_and_never_added_again():
    found = usage.summarize([session("a", turn())])
    assert found["tokens"]["thinking"] == 8 and found["tokens_total"] == 135
    assert found["seconds"] == 10.0 and found["turns"] == 1 and found["conversations"] == 1


def test_a_cost_says_what_it_is_and_unknown_is_never_zero():
    codex = turn(cost_usd=None, cost_kind="unknown", model="gpt-5")
    old_claude = {k: v for k, v in turn(cost_usd=0.25).items() if k != "cost_kind"}
    old_codex = dict({k: v for k, v in turn(cost_usd=None).items() if k != "cost_kind"}, cost_scope="unknown")
    found = usage.summarize([session("a", turn(), old_claude), session("b", codex, old_codex, connection="codex")])
    assert found["estimate_usd"] == 0.75 and found["priced_usd"] == 0.0
    assert found["unknown_cost_turns"] == 2
    assert usage.cost_of(old_claude) == (0.25, "estimate") and usage.cost_of(codex) == (None, "unknown")
    by_model = found["by"]["model"]
    assert by_model["gpt-5"]["unknown_cost_turns"] == 1 and by_model["gpt-5"]["estimate_usd"] == 0.0


def test_own_and_inclusive_are_apart_and_an_issue_counts_what_it_handed_on():
    parent = session("p", turn(), source={"label": "Chat", "ref": "jira:team:QA-142"})
    child = session("c", turn(cost_usd=0.1, model="gpt-5"), connection="codex",
                    source={"label": "Delegated", "ref": "qcr-task:e-1", "parent": "p", "record": True})
    other = session("o", turn(cost_usd=9.0))
    found = [parent, child, other]
    one = usage.conversation("unused", "p", found=found)
    assert one["own"]["estimate_usd"] == 0.5 and one["inclusive"]["estimate_usd"] == 0.6
    assert one["children"] == ["c"] and one["inclusive"]["conversations"] == 2
    issue = usage.for_ref("unused", "jira:team:QA-142", found=found)
    assert issue["own"]["turns"] == 1 and issue["inclusive"]["turns"] == 2
    assert sorted(issue["inclusive"]["by"]["model"]) == ["gpt-5", "sonnet"]


def test_kinds_cycles_periods_and_the_program_s_own_model():
    cycle_new = session("n", turn(), source={"label": "Cycle Dev · step Fix", "record": True,
                                             "ref": usage.cycle_ref("dev", "r-7", "fix")})
    cycle_old = session("d", turn(), source={"label": "Cycle Dev · step Review", "detail": "Run r-6",
                                             "record": True})
    jira = session("j", turn(model=""), source={"label": "Jira", "ref": "jira:team:QA-1", "record": True})
    later = session("l", turn(started=5000.0, ended=5001.0))
    found = [cycle_new, cycle_old, jira, later]
    assert [usage.kind_of(one) for one in found] == ["cycle", "cycle", "jira", "chat"]
    assert usage.cycle_of(cycle_new) == ("dev", "r-7", "fix") and usage.cycle_of(cycle_old) is None
    assert usage.summarize(found, kind="cycle")["conversations"] == 2
    assert usage.summarize(found, cycle="dev", run="r-7")["conversations"] == 1
    assert usage.summarize(found, since=2000)["conversations"] == 1
    assert usage.summarize(found, until=2000)["conversations"] == 3
    assert "(default)" in usage.summarize(found, by=("model", "cycle"))["by"]["model"]
    assert list(usage.summarize(found, by=("cycle",))["by"]["cycle"]) == ["dev"]


def test_the_cli_answers_from_the_project_s_record(tmp_path, capsys):
    from integrations import cli
    root = str(tmp_path / "agent-sessions")
    owner = {"id": "claude", "name": "Claude", "kind": "claude"}
    made = sessions.create(root, owner, str(tmp_path), "read", source={"label": "Chat", "ref": "jira:team:QA-9"})
    sessions.append_turn(root, made["id"], turn(), [{"type": "user", "text": "hi"}])
    child = sessions.create(root, owner, str(tmp_path), "read",
                            source={"label": "Delegated", "parent": made["id"], "record": True})
    sessions.append_turn(root, child["id"], turn(cost_usd=0.2), [{"type": "user", "text": "task"}])

    def ask(document):
        source = tmp_path / "doc.json"
        source.write_text(json.dumps(document))
        code = cli.main(["agents", "usage", "--agent-sessions-dir=" + root,
                         "--agents-file=" + str(tmp_path / "a.json"), "--from=%s" % source])
        answer = json.loads(capsys.readouterr().out)
        assert (code == 0) == answer["ok"], (code, answer)
        return answer
    issue = ask({"ref": "jira:team:QA-9"})
    assert issue["ok"] and issue["own"]["estimate_usd"] == 0.5 and issue["inclusive"]["estimate_usd"] == 0.7
    every = ask({"by": ["kind"]})
    assert every["conversations"] == 2 and sorted(every["by"]["kind"]) == ["delegated", "jira"]
    assert ask({"session": "nope"})["ok"] is False


def test_turns_qavector_started_and_tasks_for_roles_are_counted_apart():
    woken = turn(trigger="tasks_finished")
    parent = session("p", turn(), woken)
    dev = session("d", turn(model="gpt-5"), connection="codex",
                  source={"label": "Delegated", "parent": "p", "role": "dev", "record": True})
    review = session("r", turn(model="opus"), source={"label": "Delegated", "parent": "p", "role": "reviewer",
                                                      "record": True})
    found = usage.summarize([parent, dev, review], by=("role",))
    assert found["woken_turns"] == 1 and found["turns"] == 4
    assert sorted(found["by"]["role"]) == ["dev", "reviewer"]
    assert found["by"]["role"]["dev"]["turns"] == 1 and found["by"]["role"]["dev"]["conversations"] == 1
    assert usage.summarize([parent, dev, review], role="reviewer")["conversations"] == 1
