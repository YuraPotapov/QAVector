"""What changed in a conversation's folder: snapshots and the patches kept.

Real git on folders under tmp_path; no network, no agent.
"""

import json
import os
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from integrations.agents import changes                            # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="snapshots are tested on POSIX")


def _git(where, *args):
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "-c", "init.defaultBranch=main", *args],
        cwd=where, check=True, capture_output=True, text=True).stdout


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def repo(tmp_path, home):
    where = tmp_path / "shop"
    where.mkdir()
    _git(where, "init", "-q")
    _write(str(where / "app.py"), "one\ntwo\nthree\n")
    _write(str(where / "old.txt"), "going\n")
    _write(str(where / ".gitignore"), "build/\n")
    _git(where, "add", "-A")
    _git(where, "commit", "-qm", "start")
    return where


@pytest.fixture
def session_dir(tmp_path):
    where = tmp_path / "session"
    where.mkdir(mode=0o700)
    return str(where)


def _turn(session_dir, n=1):
    where = os.path.join(session_dir, "turns", "%04d" % n)
    os.makedirs(where, mode=0o700, exist_ok=True)
    return where


def _repo_state(repo):
    """What a snapshot must leave as it was: the index, refs, objects."""
    found = {}
    for path, _dirs, names in os.walk(repo / ".git"):
        for name in names:
            full = os.path.join(path, name)
            with open(full, "rb") as handle:
                found[os.path.relpath(full, repo)] = handle.read()
    return found


# ------------------------------------------------------------------ a repository
def test_live_changes_include_new_files_and_reverts_without_overwriting_history(repo, session_dir):
    before = changes.snapshot(session_dir, str(repo))
    first = _turn(session_dir, 1)
    changes.begin(session_dir, first, before)
    _write(str(repo / "app.py"), "one\nTWO\nthree\nfour\n")
    _write(str(repo / "new.md"), "hello\n")
    state = _repo_state(repo)
    live = changes.preview(session_dir, first, str(repo))
    assert live["live"] and (live["added"], live["removed"]) == (3, 1)
    assert {one["path"] for one in live["files"]} == {"app.py", "new.md"}
    assert changes.read(session_dir)["files"] == []
    after = changes.snapshot(session_dir, str(repo), layout=before)
    changes.keep(session_dir, first, str(repo), before, after)
    saved = changes.read(session_dir)
    second = _turn(session_dir, 2)
    changes.begin(session_dir, second, after)
    _write(str(repo / "app.py"), "one\ntwo\nthree\n")
    whole = changes.preview(session_dir, second, str(repo))
    turn = changes.preview(session_dir, second, str(repo), whole=False)
    assert [one["path"] for one in whole["files"]] == ["new.md"]
    assert (whole["added"], whole["removed"]) == (1, 0)
    assert (turn["added"], turn["removed"]) == (1, 2)
    assert changes.read(session_dir) == saved
    assert _repo_state(repo) == state


def test_live_readers_have_separate_indexes_from_each_other_and_the_final_snapshot(repo, session_dir):
    from concurrent.futures import ThreadPoolExecutor

    before = changes.snapshot(session_dir, str(repo))
    turn = _turn(session_dir)
    changes.begin(session_dir, turn, before)
    _write(str(repo / "new.txt"), "new\n")
    with ThreadPoolExecutor(max_workers=3) as pool:
        reads = [pool.submit(changes.preview, session_dir, turn, str(repo)) for _ in range(2)]
        final = pool.submit(changes.snapshot, session_dir, str(repo), layout=before)
        for result in reads:
            assert [one["path"] for one in result.result()["files"]] == ["new.txt"]
        assert changes.compare(session_dir, before, final.result())["added"] == 1
    assert not any(name.startswith("preview-") for name in os.listdir(changes.store(session_dir)))


def test_a_turn_in_a_repository_is_every_file_it_changed(repo, session_dir):
    before = changes.snapshot(session_dir, str(repo))
    _write(str(repo / "app.py"), "one\nTWO\nthree\nfour\n")
    _write(str(repo / "new.md"), "hello\n")
    _write(str(repo / "build" / "out.bin"), "ignored\n")
    os.unlink(repo / "old.txt")
    after = changes.snapshot(session_dir, str(repo))
    found = changes.compare(session_dir, before, after)
    by_path = {one["path"]: one for one in found["files"]}
    assert set(by_path) == {"app.py", "new.md", "old.txt"}
    assert (by_path["app.py"]["status"], by_path["app.py"]["added"],
            by_path["app.py"]["removed"]) == ("modified", 2, 1)
    assert by_path["new.md"]["status"] == "added" and by_path["old.txt"]["status"] == "deleted"
    assert (found["added"], found["removed"]) == (3, 2)
    assert "+TWO" in found["patch"] and "-two" in found["patch"]
    assert "diff --git a/app.py b/app.py" in found["patch"]
    assert found["unkept"] == [] and found["truncated"] is False


def test_the_folder_s_repository_is_left_exactly_as_it_was(repo, session_dir):
    """Not its index, its refs or its objects: a snapshot writes only into
    the conversation's own git directory."""
    _write(str(repo / "draft.py"), "untracked\n")
    _write(str(repo / "app.py"), "changed but not staged\n")
    kept = _repo_state(repo)
    before = changes.snapshot(session_dir, str(repo))
    _write(str(repo / "app.py"), "changed again\n")
    after = changes.snapshot(session_dir, str(repo))
    changes.compare(session_dir, before, after)
    assert _repo_state(repo) == kept
    assert _git(repo, "status", "--porcelain").splitlines() == [" M app.py", "?? draft.py"]


def test_nothing_the_folder_configures_is_run(repo, session_dir, tmp_path):
    """The folder is what an agent may write to. A filter, a file-system
    monitor or a diff driver it planted would run outside its restrictions if
    the snapshot used the folder's own git configuration."""
    marks = [tmp_path / name for name in ("clean", "fsmonitor", "textconv", "hook")]
    with open(repo / ".git" / "config", "a", encoding="utf-8") as handle:
        handle.write('[filter "x"]\n\tclean = touch %s\n' % marks[0])
        handle.write('[core]\n\tfsmonitor = touch %s\n' % marks[1])
        handle.write('[diff "x"]\n\ttextconv = touch %s\n' % marks[2])
        handle.write('[core]\n\thooksPath = %s\n' % (tmp_path / "hooks"))
    _write(str(tmp_path / "hooks" / "post-index-change"), "#!/bin/sh\ntouch %s\n" % marks[3])
    os.chmod(tmp_path / "hooks" / "post-index-change", 0o755)
    _write(str(repo / ".gitattributes"), "* filter=x diff=x\n")
    before = changes.snapshot(session_dir, str(repo))
    _write(str(repo / "app.py"), "changed\n")
    after = changes.snapshot(session_dir, str(repo))
    found = changes.compare(session_dir, before, after)
    assert [one["path"] for one in found["files"]] == ["app.py"]
    assert [mark.name for mark in marks if mark.exists()] == []


def test_a_folder_inside_a_repository_is_that_folder_of_it(repo, session_dir):
    _write(str(repo / "web" / "page.html"), "<p>a</p>\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "web")
    folder = str(repo / "web")
    before = changes.snapshot(session_dir, folder)
    _write(str(repo / "web" / "page.html"), "<p>b</p>\n")
    _write(str(repo / "app.py"), "outside the folder\n")
    after = changes.snapshot(session_dir, folder)
    found = changes.compare(session_dir, before, after)
    assert [one["path"] for one in found["files"]] == ["page.html"]
    assert "a/page.html" in found["patch"]


def test_a_rename_and_a_binary_file_are_said_as_such(repo, session_dir):
    before = changes.snapshot(session_dir, str(repo))
    os.rename(repo / "app.py", repo / "main.py")
    with open(repo / "logo.png", "wb") as handle:
        handle.write(bytes(range(256)) * 4)
    after = changes.snapshot(session_dir, str(repo))
    by_path = {one["path"]: one for one in changes.compare(session_dir, before, after)["files"]}
    assert by_path["main.py"]["status"] == "renamed" and by_path["main.py"]["from"] == "app.py"
    assert by_path["logo.png"]["binary"] is True and by_path["logo.png"]["added"] == 0


def test_a_repository_whose_index_cannot_be_copied_starts_from_its_commit(repo, session_dir):
    with open(repo / ".git" / "index", "wb") as handle:
        handle.write(b"not an index")
    before = changes.snapshot(session_dir, str(repo))
    _write(str(repo / "app.py"), "one\n")
    after = changes.snapshot(session_dir, str(repo))
    assert [one["path"] for one in changes.compare(session_dir, before, after)["files"]] == [
        "app.py"]


# ------------------------------------------------------------------ a plain folder
def test_a_plain_folder_and_the_repositories_in_it_are_each_compared(tmp_path, home,
                                                                     session_dir):
    folder = tmp_path / "work"
    _write(str(folder / "notes.txt"), "a\n")
    nested = folder / "claim"
    nested.mkdir()
    _git(nested, "init", "-q")
    _write(str(nested / "model.py"), "x = 1\n")
    _git(nested, "add", "-A")
    _git(nested, "commit", "-qm", "start")
    before = changes.snapshot(session_dir, str(folder))
    assert [part["kind"] for part in before["parts"]] == ["plain", "git"]
    _write(str(folder / "notes.txt"), "a\nb\n")
    _write(str(nested / "model.py"), "x = 2\n")
    after = changes.snapshot(session_dir, str(folder))
    found = changes.compare(session_dir, before, after)
    assert sorted(one["path"] for one in found["files"]) == ["claim/model.py", "notes.txt"]
    assert "diff --git a/claim/model.py b/claim/model.py" in found["patch"]


def test_a_plain_folder_too_large_lists_what_the_agent_wrote_instead(tmp_path, home,
                                                                      session_dir, monkeypatch):
    monkeypatch.setattr(changes, "MAX_FILES", 3)
    folder = tmp_path / "big"
    for n in range(5):
        _write(str(folder / ("f%d.txt" % n)), "x\n")
    before = changes.snapshot(session_dir, str(folder))
    assert before["parts"][0]["kind"] == "too_large"
    after = changes.snapshot(session_dir, str(folder))
    summary = changes.keep(session_dir, _turn(session_dir), str(folder), before, after,
                           written=[str(folder / "f1.txt"), str(folder / "f1.txt")])
    assert summary == {"files": 1, "added": 0, "removed": 0, "kept": "none"}
    found = changes.read(session_dir, _turn(session_dir))
    assert found["files"] == [{"path": "f1.txt", "status": "written", "added": 0,
                               "removed": 0, "binary": False}]
    assert found["unkept"] == ["."]


def test_a_repository_past_the_size_of_a_plain_folder_is_still_kept(tmp_path, home,
                                                                    session_dir, monkeypatch):
    """Too large is the plain part's matter: a repository further in is found
    and compared on its own."""
    monkeypatch.setattr(changes, "MAX_FILES", 2)
    folder = tmp_path / "work"
    for n in range(4):
        _write(str(folder / "a" / ("f%d.txt" % n)), "x\n")
    nested = folder / "z" / "repo"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    _write(str(nested / "m.py"), "1\n")
    before = changes.snapshot(session_dir, str(folder))
    assert [part["kind"] for part in before["parts"]] == ["too_large", "git"]
    _write(str(nested / "m.py"), "2\n")
    after = changes.snapshot(session_dir, str(folder))
    found = changes.compare(session_dir, before, after)
    assert [one["path"] for one in found["files"]] == ["z/repo/m.py"] and found["unkept"] == ["."]


def test_a_folder_of_too_many_repositories_is_not_snapshot(tmp_path, home, session_dir,
                                                           monkeypatch):
    monkeypatch.setattr(changes, "MAX_REPOSITORIES", 2)
    folder = tmp_path / "projects"
    for name in ("a", "b", "c"):
        (folder / name).mkdir(parents=True)
        _git(folder / name, "init", "-q")
    (part,) = changes.snapshot(session_dir, str(folder))["parts"]
    assert (part["kind"], part["repositories"], "tree" in part) == ("too_large", 3, False)


def test_the_snapshot_after_a_turn_takes_the_parts_of_the_one_before(repo, session_dir,
                                                                    monkeypatch):
    """The folder is looked through once a turn: after it, the same parts are
    snapshot again."""
    before = changes.snapshot(session_dir, str(repo))
    _write(str(repo / "app.py"), "changed\n")

    def looked_again(folder):
        raise AssertionError("the folder was looked through again")

    monkeypatch.setattr(changes, "parts", looked_again)
    after = changes.snapshot(session_dir, str(repo), layout=before)
    assert [one["path"] for one in changes.compare(session_dir, before, after)["files"]] == [
        "app.py"]


def test_private_folders_and_qavector_s_own_data_are_never_snapshot(tmp_path, home,
                                                                    session_dir):
    """A conversation in the home folder does not copy keys or QAVector's own
    data - the conversations themselves among it - into what it keeps."""
    _write(str(home / ".ssh" / "id_rsa"), "secret\n")
    _write(str(home / "QAVector" / "users.json"), "{}\n")
    _write(str(home / "todo.txt"), "a\n")
    before = changes.snapshot(session_dir, str(home))
    _write(str(home / ".ssh" / "id_rsa"), "another secret\n")
    _write(str(home / "QAVector" / "users.json"), '{"a": 1}\n')
    _write(str(home / "todo.txt"), "b\n")
    after = changes.snapshot(session_dir, str(home))
    found = changes.compare(session_dir, before, after)
    assert [one["path"] for one in found["files"]] == ["todo.txt"]
    assert "secret" not in found["patch"]


# ------------------------------------------------------------------ kept
def test_each_turn_and_the_conversation_so_far_are_kept_as_text(repo, session_dir):
    """The conversation's patch runs from before its first turn; both are
    files, read without git - a repository that drops objects later cannot
    take them away."""
    first = changes.snapshot(session_dir, str(repo))
    _write(str(repo / "app.py"), "one\nTWO\nthree\n")
    middle = changes.snapshot(session_dir, str(repo))
    assert changes.keep(session_dir, _turn(session_dir, 1), str(repo), first, middle) == {
        "files": 1, "added": 1, "removed": 1, "kept": "all"}
    _write(str(repo / "new.md"), "hello\n")
    last = changes.snapshot(session_dir, str(repo))
    changes.keep(session_dir, _turn(session_dir, 2), str(repo), middle, last)
    assert [one["path"] for one in changes.read(session_dir, _turn(session_dir, 2))["files"]] == [
        "new.md"]
    whole = changes.read(session_dir)
    assert sorted(one["path"] for one in whole["files"]) == ["app.py", "new.md"]
    _git(repo, "gc", "-q", "--prune=now")
    assert changes.read(session_dir)["patch"] == whole["patch"]
    for path, _dirs, names in os.walk(session_dir):
        for name in names:
            mode = stat.S_IMODE(os.stat(os.path.join(path, name)).st_mode)
            assert mode & 0o077 == 0, os.path.join(path, name)


def test_nothing_kept_reads_as_nothing(session_dir):
    assert changes.read(session_dir) == {"files": [], "patch": "", "added": 0, "removed": 0,
                                         "truncated": False, "unkept": [], "kept": ""}


def test_a_long_patch_is_cut_between_files_and_says_so(repo, session_dir, monkeypatch):
    monkeypatch.setattr(changes, "PATCH_CHARS", 300)
    before = changes.snapshot(session_dir, str(repo))
    for n in range(5):
        _write(str(repo / ("f%d.txt" % n)), "line\n" * 20)
    after = changes.snapshot(session_dir, str(repo))
    found = changes.compare(session_dir, before, after)
    assert found["truncated"] is True and len(found["patch"]) <= 300
    assert found["patch"].startswith("diff --git ") and len(found["files"]) == 5
