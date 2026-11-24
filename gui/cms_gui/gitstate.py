"""Which branch a chat's folder is on, and whether it has changes - as a
shell prompt says it: ``folder(branch)`` and a mark when something changed.

Read once per occasion - a conversation shown, a folder chosen, a turn ended -
on a thread (see ``background``), never on a timer. ``git`` is asked with
``GIT_OPTIONAL_LOCKS=0``, so a look never takes the index lock an agent's own
git command may want at the same moment.

A folder that holds several repositories (a project with its modules checked
out inside it) is told by the one where something happened last.
"""

import os
import subprocess

#: How deep under the folder repositories are looked for, and how many at most.
DEPTH, MOST = 2, 20
#: Folders never looked into for repositories: big, and never one of them.
SKIP = frozenset(("node_modules", "venv", "__pycache__", "build", "dist", "site-packages"))
#: Changed files whose times are read to tell which repository moved last.
TIMED_FILES = 300
#: How long one git command may take before the look is given up.
TIMEOUT = 5.0


def repositories(folder, depth=DEPTH, most=MOST):
    """``folder`` and the folders under it that are repositories, nearest first."""
    found, level = [], [folder]
    for deep in range(depth + 1):
        below = []
        for one in level:
            if os.path.exists(os.path.join(one, ".git")):
                found.append(one)
                if len(found) >= most:
                    return found
            if deep == depth:
                continue                     # the last level is looked at, not into
            try:
                entries = sorted(os.scandir(one), key=lambda entry: entry.name)
            except OSError:
                continue
            below.extend(entry.path for entry in entries
                         if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
                         and entry.name not in SKIP)
        level = below
    return found


def _git(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    done = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, timeout=TIMEOUT,
                          env=env, stdin=subprocess.DEVNULL)
    if done.returncode:
        raise OSError(done.stderr.decode("utf-8", "replace").strip() or "git failed")
    return done.stdout.decode("utf-8", "replace")


def branch_of(head):
    """The branch a ``## ...`` status line names, or "" when HEAD is detached."""
    head = head[3:] if head.startswith("## ") else head
    for prefix in ("No commits yet on ", "Initial commit on "):
        if head.startswith(prefix):
            return head[len(prefix):].strip()
    if head.startswith("HEAD (no branch)"):
        return ""
    return head.split("...", 1)[0].split(" [", 1)[0].strip()


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def look(repo):
    """One repository: ``{repo, branch, dirty, changed}`` - ``changed`` being
    when it last moved: a commit or checkout, or a file changed in it."""
    entries = _git(repo, "status", "--porcelain=v1", "--branch", "-z").split("\0")
    branch = branch_of(entries[0]) if entries and entries[0].startswith("## ") else ""
    if not branch:
        branch = _git(repo, "rev-parse", "--short", "HEAD").strip()
    changed_paths, rest = [], iter(entries[1:])
    for entry in rest:
        if len(entry) > 3 and entry[2] == " ":
            path = entry[3:]
            if "R" in entry[:2] or "C" in entry[:2]:
                next(rest, None)             # a rename's second entry is where it came from
            if path.endswith("/") and os.path.exists(os.path.join(repo, path, ".git")):
                continue                     # a repository inside: its changes are its own
            changed_paths.append(path)
    git_dir = os.path.join(repo, ".git")
    if os.path.isfile(git_dir):                           # a worktree or a submodule
        with open(git_dir, encoding="utf-8", errors="replace") as handle:
            pointed = handle.read().strip()
        if pointed.startswith("gitdir:"):
            git_dir = os.path.join(repo, pointed[len("gitdir:"):].strip())
    changed = max([_mtime(os.path.join(git_dir, "logs", "HEAD")), _mtime(os.path.join(git_dir, "HEAD"))]
                  + [_mtime(os.path.join(repo, path)) for path in changed_paths[:TIMED_FILES]])
    return {"repo": repo, "branch": branch, "dirty": bool(changed_paths), "changed": changed}


def state(folder):
    """The folder's repository that moved last, looked at - or None when the
    folder is none and holds none, or git cannot say."""
    folder = os.path.expanduser(str(folder or "").strip())
    if not folder or not os.path.isdir(folder):
        return None
    found = []
    for repo in repositories(folder):
        try:
            found.append(look(repo))
        except (OSError, subprocess.SubprocessError):
            continue
    if not found:
        return None
    best = max(found, key=lambda one: one["changed"])
    name = os.path.relpath(best["repo"], folder)
    return dict(best, name="" if name == "." else name, folder=folder, repositories=len(found))
