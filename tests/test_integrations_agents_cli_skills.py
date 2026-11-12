"""``--integrations agents`` for skills: what the Skills tab and the composer are answered.

The library is looked into, added to and removed from through the same door as
everything else: one JSON object per command, and a refusal with a ``kind`` a
page can act on. The repository is one the test makes; nothing is fetched from
a network and no model is asked anything.
"""

import json
import os
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cli_fixture import fake_agent, flag, starts              # noqa: E402
from integrations import cli                                        # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in CLI is a script")


def _skill(folder, name, description="What it is for."):
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: %s\ndescription: %s\n---\n" % (name, description))
    return folder


def _git(repository, *arguments):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *arguments],
        cwd=str(repository), check=True, capture_output=True, text=True).stdout.strip()


class Agents:
    def __init__(self, tmp_path, capsys, monkeypatch):
        self.tmp, self.capsys = tmp_path, capsys
        self.library = str(tmp_path / "agent-skills")
        self.folder = str(tmp_path / "work")
        os.makedirs(self.folder)
        self.log = tmp_path / "starts.jsonl"
        monkeypatch.setenv("FAKE_AGENT_LOG", str(self.log))
        self.repository = tmp_path / "upstream"
        self.repository.mkdir()
        _git(self.repository, "init", "--quiet", "-b", "main")
        _skill(self.repository / "skills" / "odoo-review", "odoo-review", "Review Odoo modules.")
        _skill(self.repository / "skills" / "test-writer", "test-writer")
        _git(self.repository, "add", "-A")
        _git(self.repository, "commit", "--quiet", "-m", "two skills")
        self.commit = _git(self.repository, "rev-parse", "HEAD")
        code, answer = self("save", document={"connection": {
            "name": "Claude", "kind": "claude", "binary": fake_agent(tmp_path)}})
        assert code == 0, answer

    def argv(self, *words, document=None):
        argv = ["agents", *words, "--agents-file=%s" % (self.tmp / "agents.json"),
                "--agent-sessions-dir=%s" % (self.tmp / "agent-sessions"),
                "--agent-skills-dir=" + self.library]
        if document is not None:
            source = self.tmp / ("from-%d.json" % len(list(self.tmp.glob("from-*.json"))))
            source.write_text(json.dumps(document), encoding="utf-8")
            argv.append("--from=%s" % source)
        return argv

    def __call__(self, *words, document=None):
        code = cli.main(self.argv(*words, document=document))
        return code, json.loads(self.capsys.readouterr().out)

    def lines(self, *words, document=None):
        code = cli.main(self.argv(*words, document=document))
        return code, [json.loads(line) for line in
                      self.capsys.readouterr().out.splitlines() if line.strip()]

    def add(self, *picks):
        code, looked = self("skill-look", document={"url": str(self.repository)})
        assert code == 0, looked
        code, answer = self("skill-add", document={"token": looked["token"],
                                                   "picks": list(picks)})
        assert code == 0, answer
        return answer["skills"]


@pytest.fixture
def agents(tmp_path, capsys, monkeypatch):
    return Agents(tmp_path, capsys, monkeypatch)


# ------------------------------------------------------------------- the library
def test_the_library_is_listed_with_what_each_agent_has_by_itself(agents, tmp_path,
                                                                 monkeypatch):
    home = tmp_path / "home"
    _skill(home / ".claude" / "skills" / "pdf", "pdf")
    (home / ".claude" / "skills" / "not-a-skill").mkdir()
    monkeypatch.setenv("HOME", str(home))
    code, answer = agents("skills")
    assert code == 0
    assert (answer["path"], answer["skills"]) == (agents.library, [])
    # For information: those every turn has whatever is chosen here.
    assert answer["own"] == {"claude": ["pdf"]}


def test_a_skill_is_looked_at_then_added_then_listed(agents):
    code, looked = agents("skill-look", document={"url": str(agents.repository)})
    assert code == 0
    assert looked["source"]["commit"] == agents.commit
    assert [one["name"] for one in looked["candidates"]] == ["odoo-review", "test-writer"]
    assert agents("skills")[1]["skills"] == []          # looking added nothing
    code, answer = agents("skill-add", document={"token": looked["token"],
                                                 "picks": ["skills/odoo-review"]})
    assert code == 0 and [one["name"] for one in answer["skills"]] == ["odoo-review"]
    (listed,) = agents("skills")[1]["skills"]
    assert listed["description"] == "Review Odoo modules."
    assert listed["source"]["path"] == "skills/odoo-review"


@pytest.mark.parametrize("document, kind", [
    ({"url": "ext::sh -c 'true'"}, "invalid"),
    ({"folder": "/no/such/folder/anywhere"}, "invalid"),
    ({"url": "/no/such/repository/anywhere"}, "unreachable"),
])
def test_what_cannot_be_looked_at_is_refused_with_a_kind(agents, document, kind):
    code, answer = agents("skill-look", document=document)
    assert (code, answer["ok"], answer["kind"]) == (1, False, kind)
    assert answer["error"]


def test_adding_again_is_refused_until_replace_says_so(agents):
    agents.add("skills/odoo-review")
    code, looked = agents("skill-look", document={"url": str(agents.repository)})
    assert looked["candidates"][0]["installed"] is True
    document = {"token": looked["token"], "picks": ["skills/odoo-review"]}
    code, answer = agents("skill-add", document=document)
    assert (code, answer["kind"]) == (1, "exists")
    code, answer = agents("skill-add", document=dict(document, replace=True))
    assert code == 0


def test_updating_says_what_changed_and_takes_it_only_when_told(agents):
    agents.add("skills/odoo-review")
    (agents.repository / "skills" / "odoo-review" / "notes.md").write_text("More.\n")
    _git(agents.repository, "add", "-A")
    _git(agents.repository, "commit", "--quiet", "-m", "notes")
    code, again = agents("skill-update", "odoo-review")
    assert code == 0
    assert again["changes"] == {"added": ["notes.md"], "removed": [], "changed": []}
    assert again["was"] == agents.commit and again["now"] != agents.commit
    assert agents("skills")[1]["skills"][0]["files"] == ["SKILL.md"]
    code, answer = agents("skill-add", document={"token": again["token"], "picks": [""],
                                                 "replace": True})
    assert code == 0
    assert agents("skills")[1]["skills"][0]["files"] == ["SKILL.md", "notes.md"]


def test_a_skill_is_removed_and_one_that_is_not_there_says_so(agents):
    agents.add("skills/odoo-review")
    assert agents("skill-remove", "odoo-review") == (0, {"ok": True, "name": "odoo-review"})
    code, answer = agents("skill-remove", "odoo-review")
    assert (code, answer["kind"]) == (1, "not_found")
    code, answer = agents("skill-update", "odoo-review")
    assert (code, answer["kind"]) == (1, "not_found")
    code, answer = agents("skill-remove", "../../etc")
    assert (code, answer["kind"]) == (1, "invalid")


@pytest.mark.parametrize("words", [("skill-look",), ("skill-add",), ("skill-update",),
                                   ("skill-remove",), ("skill-remove", "a", "b")])
def test_a_wrong_command_line_is_answered_with_the_usage(agents, words):
    code, answer = agents(*words)
    assert (code, answer["kind"]) == (2, "usage")
    assert "skill-look --from=F" in answer["usage"]


# ----------------------------------------------------------- skills in a conversation
def test_a_conversation_is_started_with_skills_and_takes_them_on_its_turns(agents):
    agents.add("skills/odoo-review", "skills/test-writer")
    code, answer = agents("session-new", document={
        "connection": "claude", "folder": agents.folder, "skills": ["odoo-review"]})
    assert code == 0 and answer["session"]["skills"] == ["odoo-review"]
    session = answer["session"]["id"]
    code, lines = agents.lines("send", session, document={"message": "review it"})
    assert code == 0 and lines[-1]["turn"]["skills"] == ["odoo-review"]
    assert flag(starts(agents.log)[0], "--plugin-dir")
    # Changed for the conversation, and for one turn.
    code, answer = agents("session-set", session, document={"skills": ["test-writer"]})
    assert answer["session"]["skills"] == ["test-writer"]
    code, lines = agents.lines("send", session, document={"message": "again", "skills": []})
    assert lines[-1]["turn"]["skills"] == [] and "--plugin-dir" not in starts(agents.log)[1]["argv"]
    assert [one["skills"] for one in agents("activity")[1]["activity"]] == [[], ["odoo-review"]]


@pytest.mark.parametrize("skills, said", [
    (["no-such"], "The skill 'no-such' is not in the library any more."),
    ("odoo-review", "Skills is a list of names from the library."),
    (["../outside"], "is not a skill's name"),
])
def test_a_skill_the_library_does_not_have_cannot_be_chosen(agents, skills, said):
    agents.add("skills/odoo-review")
    code, answer = agents("session-new", document={
        "connection": "claude", "folder": agents.folder, "skills": skills})
    assert (code, answer["kind"]) == (1, "invalid") and said in answer["error"]
    assert agents("sessions")[1]["total"] == 0


def test_a_turn_naming_a_skill_removed_since_is_refused_before_it_starts(agents):
    agents.add("skills/odoo-review")
    session = agents("session-new", document={
        "connection": "claude", "folder": agents.folder,
        "skills": ["odoo-review"]})[1]["session"]["id"]
    agents("skill-remove", "odoo-review")
    code, lines = agents.lines("send", session, document={"message": "hello"})
    assert code == 1 and len(lines) == 1
    assert lines[0]["kind"] == "skills" and "'odoo-review'" in lines[0]["error"]
    assert starts(agents.log) == []


# ----------------------------------------------------------------- export, import
def _export(agents, connections=(), skills=()):
    target = agents.tmp / ("export-%d.json" % len(list(agents.tmp.glob("export-*.json"))))
    code, answer = agents("export", "--to=%s" % target,
                          document={"connections": list(connections), "skills": list(skills)})
    return code, answer, target


def test_connections_and_skills_travel_whole_in_a_private_file(agents):
    agents.add("skills/odoo-review")
    script = os.path.join(agents.library, "odoo-review", "check.sh")
    with open(script, "w") as handle:
        handle.write("#!/bin/sh\necho ok\n")
    os.chmod(script, 0o755)
    code, answer, target = _export(agents, ["claude"], ["odoo-review"])
    assert code == 0, answer
    assert (answer["connections"], answer["skills"]) == (["claude"], ["odoo-review"])
    assert stat.S_IMODE(os.stat(str(target)).st_mode) == 0o600
    document = json.loads(target.read_text())
    (connection,) = document["agent_connections"]
    assert connection["id"] == "claude" and "last_check" not in connection
    (skill,) = document["agent_skills"]
    assert sorted(one["path"] for one in skill["files"]) == ["SKILL.md", "check.sh"]
    assert skill["source"]["commit"] == agents.commit
    # Into another installation: the same skill, byte for byte, still runnable.
    other = agents.tmp / "elsewhere"
    code = cli.main(["agents", "import", "--from=%s" % _with_mode(target, "merge"),
                     "--agents-file=%s" % (other / "agents.json"),
                     "--agent-skills-dir=%s" % (other / "agent-skills")])
    said = json.loads(agents.capsys.readouterr().out)["said"]
    assert code == 0 and said[1] == "Skill odoo-review added."
    copied = other / "agent-skills" / "odoo-review"
    assert (copied / "check.sh").read_text() == "#!/bin/sh\necho ok\n"
    assert os.access(str(copied / "check.sh"), os.X_OK)
    assert json.loads((other / "agent-skills" / "odoo-review.source.json").read_text())[
        "commit"] == agents.commit
    assert [one["id"] for one in json.loads((other / "agents.json").read_text())[
        "connections"]] == ["claude"]


def _with_mode(target, mode):
    document = json.loads(target.read_text())
    document["mode"] = mode
    path = target.with_name(target.stem + "-" + mode + ".json")
    path.write_text(json.dumps(document))
    return path


def test_what_is_here_already_is_left_alone_or_added_beside_it(agents):
    agents.add("skills/odoo-review")
    _code, _answer, target = _export(agents, ["claude"], ["odoo-review"])
    code, answer = agents("import", document=dict(json.loads(target.read_text()),
                                                  mode="merge"))
    assert answer["said"] == ["Agent connection Claude is here already; left as it is.",
                              "Skill odoo-review is in the library already; left as it is."]
    code, answer = agents("import", document=dict(json.loads(target.read_text()),
                                                  mode="copy"))
    assert answer["said"] == ["Agent connection Claude (copy) added.",
                              "Skill odoo-review-copy added beside odoo-review."]
    assert [one["name"] for one in agents("skills")[1]["skills"]] == [
        "odoo-review", "odoo-review-copy"]
    assert [one["name"] for one in agents("list")[1]["connections"]] == [
        "Claude", "Claude (copy)"]


def test_a_program_that_is_not_on_this_computer_is_found_on_path_instead(agents):
    code, answer = agents("import", document={"agent_connections": [
        {"id": "laptop", "name": "Laptop", "kind": "claude",
         "binary": "/no/such/place/claude"}]})
    assert answer["said"] == ["Agent connection Laptop added; its program "
                              "/no/such/place/claude is not on this computer, so it is "
                              "found on PATH."]
    (one,) = [c for c in agents("list")[1]["connections"] if c["id"] == "laptop"]
    assert one["binary"] == ""


@pytest.mark.parametrize("path", ["../outside.md", "/etc/cron.d/x", "a/../../b", "",
                                  "C:/windows/x", "./SKILL.md"])
def test_a_file_of_an_export_can_only_land_inside_its_skill(agents, path):
    import base64
    data = base64.b64encode(b"x").decode()
    code, answer = agents("import", document={"agent_skills": [{
        "name": "sneaky", "files": [{"path": "SKILL.md", "data": data},
                                    {"path": path, "data": data}]}]})
    assert code == 0 and answer["said"][0].startswith("Skill sneaky was not imported")
    assert not os.path.exists(os.path.join(agents.library, "sneaky"))
    assert not (agents.tmp / "outside.md").exists()
    assert os.listdir(os.path.join(agents.library, ".pending")) == []


def test_a_skill_without_its_instructions_is_not_imported(agents):
    import base64
    code, answer = agents("import", document={"agent_skills": [{
        "name": "notes", "files": [{"path": "notes.md",
                                    "data": base64.b64encode(b"x").decode()}]}]})
    assert "There is no SKILL.md" in answer["said"][0]
    assert agents("skills")[1]["skills"] == []


def test_export_needs_to_know_where_and_what(agents):
    code, answer = agents("export", document={"connections": []})
    assert (code, answer["kind"]) == (2, "usage")
    code, answer, _target = _export(agents, [], ["no-such"])
    assert code == 1 and answer["kind"] == "invalid"
