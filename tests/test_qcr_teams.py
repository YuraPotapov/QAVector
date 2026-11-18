"""Teams through QCR (ADR-015 T.2): what an agent sees of a channel, and how far it may post.

End to end with the project's own files, the real Engine and journal, and a
loopback Workflows webhook:

* the channel's own access is the ceiling: Agents Off hides it from every
  agent - delegated tasks too; Ask waits for the person's Approve; Post sends
  without asking. The agent's switch only narrows it;
* the level is read again before each post, so a change made during a turn
  holds for its next post;
* ``describe`` gives the agent everything set on the channel but its webhook;
* one post a minute holds for agents as for everybody; the webhook's address is
  nowhere an agent, a card, the journal or the history can show it.
"""

import json
import os
import sys

import pytest

from integrations import reader as reader_mod, store
from integrations.agents.store import ACCESS_GRANTS
from qcr import Authority, Grant, QCRError, Registry
from qcr.adapters.delegation import child_grants
from qcr.adapters.integrations import IntegrationAdapter, kind_of
from qcr.engine import Engine
from qcr.executions import ExecutionStore

sys.path.insert(0, os.path.dirname(__file__))
from test_integrations_teams import SECRET_PATH, Webhook  # noqa: E402

TEAMS = ACCESS_GRANTS["teams"]


class World:
    def __init__(self, folder, agents="approve", cycles="post", others=()):
        self.hook = Webhook()
        self.paths = (str(folder / "integrations.json"), str(folder / "cyclesecrets.json"),
                      str(folder / "environments.json"))
        self.ids = {}
        for name, level in (("Release", agents),) + tuple(others):
            entry, problems = store.upsert({"provider": "teams", "name": name, "settings": {
                "footer": "via QAVector", "aliases": {"Dana": "dana.k@team.example"},
                "access": {"agents": level, "cycles": cycles}}},
                self.paths[0], self.paths[1], new_token=self.hook.url)
            assert not problems
            self.ids[name] = entry["id"]
        self.cid = self.ids["Release"]
        self.authority = Authority()
        self.registry = Registry(self.authority)
        self.store = ExecutionStore(str(folder / "executions.sqlite"))
        self.engine = Engine(self.registry, self.store)
        self.told = []
        self.adapter = IntegrationAdapter(self.registry, "p", reader_mod.ProjectReader(*self.paths),
                                          notify=self.told.append, label=lambda actor: "Agent Claude",
                                          spawn=lambda work: work())
        self.adapter.refresh()
        self.agent = self.caller("agent:claude/s1")

    def caller(self, actor, permissions=TEAMS):
        """Issued as the host issues it: through the connections' ceilings."""
        grants = self.adapter.narrow(actor, [Grant("p", one) for one in permissions])
        return self.authority.issue(actor, grants)

    def capability(self, op, name="Release"):
        return "integrations.teams.%s.%s" % (self.ids[name], op)

    def read(self, op, request=None, caller=None, name="Release"):
        return self.registry.read(caller or self.agent, "p", self.capability(op, name), request or {})

    def post(self, text="Release 1.4 is out", caller=None, **more):
        return self.engine.invoke(caller or self.agent, "p", self.capability("post"), dict(more, text=text))

    def get(self, execution_id):
        return self.engine.get(self.agent, execution_id)

    def set_agents(self, level):
        entry = next(one for one in store.load(self.paths[0]) if one["id"] == self.cid)
        entry["settings"]["access"]["agents"] = level
        _saved, problems = store.upsert(entry, self.paths[0], self.paths[1])
        assert not problems

    def close(self):
        self.engine.close()
        self.store.close()
        self.hook.close()


@pytest.fixture
def world(tmp_path):
    made = []

    def build(**kwargs):
        folder = tmp_path / str(len(made))
        folder.mkdir()
        made.append(World(folder, **kwargs))
        return made[-1]

    yield build
    for one in made:
        one.close()


def _visible(w, caller):
    return sorted(cap.capability_id.split(".")[2] for cap in w.registry.list_capabilities(caller, "p")
                  if cap.capability_id.startswith("integrations.teams."))


# -- who sees what --------------------------------------------------------------------
def test_kinds_of_caller_by_their_actor_id():
    assert kind_of("agent:claude/s1") == "agents"
    assert kind_of("cycle:r1") == kind_of("run:launcher") == "cycles"
    assert kind_of("gui-user") is None


def test_an_agent_sees_only_the_channels_that_let_agents_in(world):
    w = world(others=(("Management", "off"), ("Team", "post")))
    assert set(_visible(w, w.agent)) == {w.ids["Release"], w.ids["Team"]}
    with pytest.raises(QCRError) as refused:
        w.read("describe", name="Management")
    assert refused.value.code in ("not_found", "authorization_denied")


def test_the_person_is_held_to_no_channel_s_ceiling(world):
    w = world(others=(("Management", "off"),))
    person = w.caller("gui-user")
    assert set(_visible(w, person)) == {w.ids["Release"], w.ids["Management"]}


def test_without_the_agent_s_switch_no_channel_is_seen_whatever_it_allows(world):
    w = world(agents="post")
    assert _visible(w, w.caller("agent:codex/s2", permissions=ACCESS_GRANTS["jira"])) == []


def test_a_delegated_task_cannot_reach_a_channel_its_parent_could_not(world):
    w = world(others=(("Management", "off"),))
    parent = w.adapter.narrow("agent:claude/s1", [Grant("p", one) for one in TEAMS])
    handed = child_grants([{"permission": g.permission, "resource_id": g.resource_id} for g in parent],
                          {"qavector_access": {"teams": True}})
    child = w.authority.issue("agent:codex/child", w.adapter.narrow(
        "agent:codex/child", [Grant("p", one["permission"], one.get("resource_id")) for one in handed]))
    assert _visible(w, child) == [w.cid] * len(_visible(w, child)) and _visible(w, child)


# -- describe -------------------------------------------------------------------------
def test_describe_gives_everything_set_on_the_channel_but_its_webhook(world):
    w = world()
    found = w.read("describe")
    assert found["name"] == "Release" and found["posts_to"] == "channel"
    assert found["footer"] == "via QAVector" and found["mention_names"] == {"Dana": "dana.k@team.example"}
    assert found["access"] == {"agents": "approve", "cycles": "post"}
    assert found["posting"]["one_post_every_seconds"] == 60 and found["posting"]["next_post_in_seconds"] == 0
    assert found["posting"]["takes_back"] is False and found["posts_kept"] == 0
    assert SECRET_PATH not in json.dumps(found) and str(w.hook.server.server_port) not in json.dumps(found)


# -- posting -----------------------------------------------------------------------------
def test_ask_a_post_waits_for_approve_and_the_approved_text_is_sent(world):
    w = world(agents="approve")
    execution = w.post("Draft text", mentions=["Dana"])
    assert execution["state"] == "dispatched" and w.hook.got == []
    [told] = w.told
    assert told["type"] == "integration_approval" and told["card"]["title"] == "Post to Release"
    assert "cannot be taken back" in told["card"]["notes"][0]
    assert sorted(told["editable"]) == ["text", "title"]
    assert w.adapter.writer.answer(execution["execution_id"], True, "you", {"text": "Approved text"})
    done = w.get(execution["execution_id"])
    assert done["state"] == "succeeded", done
    [sent] = w.hook.got
    assert "Approved text" in json.dumps(sent) and "Draft" not in json.dumps(sent)
    [posted] = w.read("history")["items"]
    assert posted["approved_by"] == "you" and posted["source"]["label"] == "Agent Claude"
    assert posted["source"]["detail"] == execution["execution_id"]
    everything = json.dumps([w.told, done, w.read("history"), w.engine.subscribe(
        w.agent, execution["execution_id"])["events"]])
    assert SECRET_PATH not in everything


def test_a_denied_post_is_never_sent(world):
    w = world(agents="approve")
    execution = w.post()
    w.adapter.writer.answer(execution["execution_id"], False, "you")
    assert w.get(execution["execution_id"])["error"]["code"] == "denied"
    assert w.hook.got == [] and w.read("history")["items"] == []


def test_post_level_sends_without_asking(world):
    w = world(agents="post")
    execution = w.post("Straight out")
    done = w.get(execution["execution_id"])
    assert done["state"] == "succeeded" and w.told == []
    assert len(w.hook.got) == 1
    [posted] = w.read("history")["items"]
    assert "approved_by" not in posted and posted["outcome"] == "accepted"


def test_a_channel_turned_off_during_a_turn_refuses_the_next_post(world):
    w = world(agents="post")
    w.set_agents("off")
    execution = w.post()
    done = w.get(execution["execution_id"])
    assert done["state"] == "failed" and done["error"]["code"] == "authorization_denied"
    assert w.hook.got == []


def test_a_channel_moved_from_post_to_ask_asks_from_then_on(world):
    w = world(agents="post")
    w.set_agents("approve")
    execution = w.post()
    assert w.get(execution["execution_id"])["state"] == "dispatched" and w.hook.got == []


def test_one_post_a_minute_holds_for_agents_too(world):
    w = world(agents="post")
    assert w.get(w.post("first")["execution_id"])["state"] == "succeeded"
    second = w.get(w.post("second")["execution_id"])
    assert second["state"] == "failed" and second["error"]["code"] == "refused"
    assert len(w.hook.got) == 1
    assert w.read("describe")["posting"]["next_post_in_seconds"] > 0


# -- through the host --------------------------------------------------------------------
def test_the_host_holds_every_actor_it_issues_to_the_channels_ceilings(tmp_path):
    """Not only the adapter: the grants the GUI sends for a turn go through it."""
    from qcr.host import Host
    paths = (str(tmp_path / "integrations.json"), str(tmp_path / "secrets.json"), str(tmp_path / "env.json"))
    ids = {}
    for name, level in (("Release", "approve"), ("Management", "off")):
        entry, problems = store.upsert({"provider": "teams", "name": name,
                                        "settings": {"access": {"agents": level}}},
                                       paths[0], paths[1], new_token="https://example.invalid" + SECRET_PATH)
        assert not problems
        ids[name] = entry["id"]
    host = Host("p", str(tmp_path / "journal.sqlite"), lambda line: None)
    try:
        host.open_integrations(*paths)
        host._on_hello({"protocol": 1, "actors": {}})
        host._on_actor({"actor": "agent:claude/s1", "grants": [{"permission": p} for p in TEAMS]})

        def read(name):
            return host.handle_agent("agent:claude/s1", {"type": "read", "capability": "integrations.teams.%s.%s"
                                                         % (ids[name], "describe"), "input": {}})
        assert read("Release")["ok"]
        assert not read("Management")["ok"]
    finally:
        host.close()


def test_an_agent_s_post_is_one_line_in_activity_without_its_words(world):
    from integrations import activity
    w = world(agents="post")
    w.post("Words nobody should log")
    [line] = activity.read(w.paths[0])
    assert line["op"] == "post" and line["ok"] is True and line["source"]["label"] == "Agent Claude"
    assert line["summary"] == "Post to Release: accepted (HTTP 202)"
    assert "Words nobody" not in json.dumps(line) and SECRET_PATH not in json.dumps(line)


def test_activity_says_whose_change_each_line_is(world):
    from integrations import activity
    w = world(agents="post")
    w.post()
    [line] = activity.read(w.paths[0])
    assert line["provider"] == "teams"
