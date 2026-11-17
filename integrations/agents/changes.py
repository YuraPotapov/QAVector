"""What changed in a conversation's folder while the agent worked in it.

A turn is bracketed by two snapshots of the folder, and what it changed is the
difference between them - every file, whoever touched it: an edit the agent
made with a tool, a file a command it ran wrote, and also a change somebody
else made in the folder at the same time. It is said so wherever it is shown.

**A snapshot is a git tree, in a git directory of the conversation's own.**
Git already knows how to hash a folder cheaply and tell two states apart; what
it must not be given is the folder's own repository to do it with. A
repository's configuration can name programs to run - a clean filter on
``add``, a file-system monitor, a diff driver - and the folder is exactly what
the agent could write to: an agent allowed only to edit files would have a
program of its choosing run, outside its restrictions, by the snapshot after
its turn. So every git command here runs against ``<session>/changes/git``
with no system or global configuration either, and reads the folder's
repository only as objects (``GIT_ALTERNATE_OBJECT_DIRECTORIES``): data, never
configuration. Nothing of the folder's repository is written - not its index,
its refs or its objects. QAVector's own data and the private folders a
restricted program never sees (``.ssh`` and the like) are never snapshot, even
inside the folder.

**Fast because of an index kept between snapshots.** The first snapshot of a
repository starts from a copy of the repository's own index, whose file
details let ``git add -A`` hash only what differs from it; every later one
starts from the index the last snapshot left. A folder that is not a
repository starts empty, and is snapshot only up to :data:`MAX_FILES` files and
:data:`MAX_BYTES` bytes - past that, what changed in it is not kept, and the
files the agent wrote there are listed instead.

**One folder, several parts.** A folder inside a repository is that
repository, narrowed to the folder. A folder that is not inside one is itself
one part, and each repository found within it is another - a repository nested
in a plain folder would otherwise be one opaque entry.

**Kept as text.** After each turn its patch, and the whole conversation's so
far, are written out as files; reading them never runs git, and a repository
that has since dropped objects cannot take an old patch with it.

During a turn, readers compare its published baseline against a live snapshot
using temporary private indexes. These previews never replace saved patches.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time

import runtime_paths

DIR = "changes"

#: Git's own name for the tree with nothing in it: what a repository that
#: appeared during a conversation had before, so all of it shows as added.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: How much of a folder that is not a repository is kept. Past either, the
#: folder is too large to snapshot every turn.
MAX_FILES = 20000
MAX_BYTES = 200 * 1024 * 1024

#: How many entries are looked at while finding repositories in a folder and
#: counting it - a home folder is not walked to its end before every turn.
MAX_WALK = 200000

#: How deep a folder too large to keep is looked through for repositories.
SEARCH_DEPTH = 3

#: How many repositories a folder may hold and still be snapshot every turn -
#: each is a ``git add`` before and after it. A folder of all one's projects
#: is past it: what changed there is listed from the files the agent wrote.
MAX_REPOSITORIES = 20

#: How long a patch kept for reading may be; past it, whole files are left out
#: and the answer says so.
PATCH_CHARS = 5 * 1024 * 1024

GIT_TIMEOUT = 300

#: Private folders under the home folder a restricted program never sees, and
#: a snapshot never copies.
PRIVATE = (".ssh", ".gnupg", ".aws", ".netrc", ".docker")

STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied",
          "T": "modified"}


class ChangesError(Exception):
    """A snapshot or a comparison that could not be made."""


def store(session_dir):
    return os.path.join(session_dir, DIR)


# -- what a folder is made of ---------------------------------------------------
def _never(folder):
    """Paths inside ``folder`` that are never snapshot."""
    home = os.path.expanduser("~")
    roots = [runtime_paths.user_data_root(), os.path.join(home, runtime_paths.USER_DIR_NAME)]
    roots += [os.path.join(home, one) for one in PRIVATE]
    found = []
    for root in roots:
        root = os.path.realpath(root)
        if root == folder or root.startswith(folder + os.sep):
            found.append(root)
    return found


def _git_dir(path):
    """``(git dir, common dir)`` of a work tree whose top is ``path``, read off
    the files - asking git would mean running it against the repository."""
    dot = os.path.join(path, ".git")
    if os.path.isdir(dot):
        gitdir = dot
    elif os.path.isfile(dot):
        try:
            with open(dot, encoding="utf-8") as handle:
                said = handle.read().strip()
        except OSError:
            return None
        if not said.startswith("gitdir:"):
            return None
        gitdir = os.path.normpath(os.path.join(path, said[len("gitdir:"):].strip()))
    else:
        return None
    if not os.path.isfile(os.path.join(gitdir, "HEAD")):
        return None
    common = gitdir
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as handle:
            common = os.path.normpath(os.path.join(gitdir, handle.read().strip()))
    except OSError:
        pass
    if not os.path.isdir(os.path.join(common, "objects")):
        return None
    return gitdir, common


def repository(folder):
    """The top of the git work tree ``folder`` is in, or ""."""
    path = os.path.realpath(folder)
    while True:
        if _git_dir(path):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return ""
        path = parent


def parts(folder):
    """What a snapshot of ``folder`` is made of. Each part is a dict:

    ``key``     names it across snapshots
    ``kind``    ``git``, ``plain`` or ``too_large``
    ``root``    the folder git works in for it
    ``within``  for a repository the folder is inside of, the folder in it
    ``show``    where the part is in the folder, for the paths shown
    """
    folder = os.path.realpath(folder)
    top = repository(folder)
    if top:
        within = os.path.relpath(folder, top)
        return [{"key": "git:" + top, "kind": "git", "root": top,
                 "within": "" if within == "." else within, "show": ""}]
    nested, files, size, complete = _walk(folder)
    plain = {"key": "plain:" + folder, "kind": "plain", "root": folder, "within": "",
             "show": "", "skip": [os.path.relpath(one, folder) for one in nested]}
    if len(nested) > MAX_REPOSITORIES:
        return [dict(plain, kind="too_large", repositories=len(nested), skip=[])]
    if not complete:
        plain.update(kind="too_large", files=files, bytes=size)
    found = [plain]
    for nested_top in nested:
        found.append({"key": "git:" + nested_top, "kind": "git", "root": nested_top,
                      "within": "", "show": os.path.relpath(nested_top, folder)})
    return found


def _walk(folder):
    """``(repositories inside, files outside them, their bytes, small enough)``.

    A folder too large to keep is still looked through for the repositories in
    it - each is kept on its own whatever the rest weighs - but only to
    :data:`SEARCH_DEPTH` folders down: repositories sit near the top of a
    folder, and a data folder's depths (a file store, a virtualenv) are where
    a walk would spend its time.
    """
    never = set(_never(folder))
    nested, files, size, seen, small = [], 0, 0, 0, True
    base = folder.rstrip(os.sep).count(os.sep)
    for path, directories, names in os.walk(folder):
        depth = path.rstrip(os.sep).count(os.sep) - base
        keep = []
        for name in sorted(directories):
            full = os.path.join(path, name)
            if name == ".git" or full in never or os.path.islink(full):
                continue
            if _git_dir(full):
                nested.append(full)
            else:
                keep.append(name)
        seen += len(names) + len(keep)
        files += len(names)
        if small:
            for name in names:
                try:
                    size += os.lstat(os.path.join(path, name)).st_size
                except OSError:
                    pass
            small = files <= MAX_FILES and size <= MAX_BYTES
        # Its subfolders were looked at for a repository just now; below them
        # only a folder still small enough to keep is walked.
        directories[:] = keep if small or depth < SEARCH_DEPTH else []
        if seen > MAX_WALK:
            return nested, files, size, False
    return nested, files, size, small


# -- git, on the conversation's own git directory --------------------------------
class Git:
    def __init__(self, session_dir, run=None):
        self.where = store(session_dir)
        self.git_dir = os.path.join(self.where, "git")
        self.run_ = run or subprocess.run
        if not os.path.isdir(self.git_dir):
            os.makedirs(self.where, mode=0o700, exist_ok=True)
            self(["init", "-q", "--bare"])
            for key, value in (("gc.auto", "0"), ("core.autocrlf", "false"),
                               ("core.safecrlf", "false"), ("core.quotepath", "false")):
                self(["config", key, value])

    def __call__(self, args, index=None, objects=None, work_tree=None):
        env = {key: value for key, value in runtime_paths.clean_subprocess_env().items()
               if not key.startswith("GIT_")}
        env.update(GIT_DIR=self.git_dir, GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
        if index:
            env["GIT_INDEX_FILE"] = index
        if objects:
            env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = objects
        argv = ["git", "-c", "core.bare=false", "-c", "core.fsmonitor=false",
                "-c", "core.splitIndex=false", "-c", "core.untrackedCache=false",
                "-c", "core.hooksPath=" + os.devnull]
        if work_tree:
            argv += ["--work-tree", work_tree]
        try:
            # Owner-only, as everything a conversation keeps: git makes its
            # files as the umask lets it.
            done = self.run_(argv + list(args), cwd=work_tree or self.where, env=env,
                             capture_output=True, timeout=GIT_TIMEOUT,
                             stdin=subprocess.DEVNULL, umask=0o077)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ChangesError("git %s: %s" % (_command(args), exc)) from None
        if done.returncode != 0:
            said = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ChangesError("git %s: %s" % (_command(args), said[-1] if said
                                               else "exit %s" % done.returncode))
        return done.stdout.decode("utf-8", "replace")


def _command(args):
    return next((one for one in args if not one.startswith("-") and "=" not in one), "")


def _objects(root):
    found = _git_dir(root)
    return os.path.join(found[1], "objects") if found else None


def _part_file(git, part, suffix):
    name = hashlib.sha1(part["key"].encode("utf-8")).hexdigest()[:16]
    return os.path.join(getattr(git, "scratch", None) or git.where, "%s.%s" % (name, suffix))


# -- snapshots ------------------------------------------------------------------
def snapshot(session_dir, folder, run=None, layout=None, scratch=None):
    """The folder now: ``{"at", "parts"}``, each part with its ``tree`` - or,
    for one too large to keep, without. ``layout`` is an earlier snapshot whose
    parts to take again rather than looking through the folder anew - the
    turn's own first one, for the snapshot after it. Raises
    :class:`ChangesError`."""
    git = Git(session_dir, run)
    git.scratch = scratch
    taken = []
    for part in (layout or {}).get("parts") or parts(folder):
        part = {key: value for key, value in part.items() if key != "tree"}
        if part["kind"] == "too_large":
            taken.append(part)
            continue
        objects = _objects(part["root"]) if part["kind"] == "git" else None
        index = _index(git, part, objects)
        exclude = _exclude(git, part)
        try:
            git(["-c", "core.excludesFile=" + exclude, "add", "-A", "--ignore-errors",
                 "--", part["within"] or "."], index=index, objects=objects,
                work_tree=part["root"])
        except ChangesError:
            # ``--ignore-errors`` still ends in failure for a file it could
            # not read; everything it could read is in the index all the same.
            pass
        part["tree"] = git(["write-tree"], index=index, objects=objects).strip()
        taken.append(part)
    return {"at": time.time(), "parts": taken}


def _index(git, part, objects):
    """The index a part's snapshot starts from: the last snapshot's; else, for
    a repository, a copy of the repository's own; else - a copy this git
    cannot read - the commit the repository has checked out."""
    index = _part_file(git, part, "index")
    if getattr(git, "scratch", None):
        # Git replaces its index atomically. Copy the last cached index into
        # this reader's private directory; never contend with the turn writer
        # or another live reader for an index/exclude file.
        cached = os.path.join(git.where, os.path.basename(index))
        if os.path.isfile(cached):
            shutil.copyfile(cached, index)
            os.chmod(index, 0o600)
    if os.path.isfile(index) or part["kind"] != "git":
        return index
    gitdir = _git_dir(part["root"])[0]
    try:
        shutil.copyfile(os.path.join(gitdir, "index"), index)
        os.chmod(index, 0o600)
        # Read through once - a path nothing has - to know this git can.
        git(["ls-files", "--", ".qavector-reads-the-index"], index=index, objects=objects,
            work_tree=part["root"])
        return index
    except (OSError, ChangesError):
        try:
            os.unlink(index)
        except OSError:
            pass
    head = _head(part["root"])
    if head:
        try:
            git(["read-tree", head], index=index, objects=objects)
        except ChangesError:
            pass
    return index


def _head(top):
    """The commit a work tree has checked out, read off its files."""
    found = _git_dir(top)
    if not found:
        return ""
    gitdir, common = found
    try:
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as handle:
            said = handle.read().strip()
    except OSError:
        return ""
    if not said.startswith("ref:"):
        return said
    ref = said[4:].strip()
    for base in (gitdir, common):
        try:
            with open(os.path.join(base, ref), encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            continue
    try:
        with open(os.path.join(common, "packed-refs"), encoding="utf-8") as handle:
            for line in handle:
                sha, _, name = line.strip().partition(" ")
                if name == ref:
                    return sha
    except OSError:
        pass
    return ""


def _exclude(git, part):
    """What a part leaves out besides the folder's own .gitignore files: the
    repository's own exclusions (copied: data, not configuration), the
    repositories nested in a plain folder, and what is never snapshot."""
    lines = []
    found = _git_dir(part["root"]) if part["kind"] == "git" else None
    if found:
        try:
            with open(os.path.join(found[1], "info", "exclude"), encoding="utf-8") as handle:
                lines += handle.read().splitlines()
        except OSError:
            pass
    root = os.path.realpath(part["root"])
    lines += ["/" + _escape(os.path.relpath(one, root)) for one in _never(root)]
    lines += ["/" + _escape(one) + "/" for one in part.get("skip") or []]
    path = _part_file(git, part, "exclude")
    _write(path, "\n".join(lines) + "\n")
    return path


def _escape(path):
    return "".join("\\" + one if one in "*?[]\\!# " else one
                   for one in path.replace(os.sep, "/"))


# -- what changed ---------------------------------------------------------------
def compare(session_dir, before, after, run=None):
    """What changed between two snapshots: ``{"files", "patch", "added",
    "removed", "truncated", "unkept"}``. ``files`` are dicts with ``path``,
    ``status`` (added, modified, deleted, renamed), ``added``, ``removed``,
    ``binary`` and, for a rename, ``from``; ``unkept`` says where in the folder
    a part was too large to compare."""
    git = Git(session_dir, run)
    earlier = {part["key"]: part for part in (before or {}).get("parts") or []}
    files, patches, unkept = [], [], []
    for part in (after or {}).get("parts") or []:
        was = earlier.get(part["key"])
        if not part.get("tree") or (was is not None and not was.get("tree")):
            unkept.append(part.get("show") or ".")
            continue
        old = (was or {}).get("tree") or EMPTY_TREE
        if old == part["tree"]:
            continue
        objects = _objects(part["root"]) if part["kind"] == "git" else None
        files += _files(git, part, old, objects)
        patches.append(_patch(git, part, old, objects))
    patch = "".join(patches)
    truncated = len(patch) > PATCH_CHARS
    if truncated:
        cut = patch.rfind("\ndiff --git ", 0, PATCH_CHARS)
        patch = patch[:cut + 1] if cut > 0 else patch[:PATCH_CHARS]
    return {"files": files, "patch": patch, "truncated": truncated, "unkept": unkept,
            "added": sum(one["added"] for one in files),
            "removed": sum(one["removed"] for one in files)}


def _diff(part, old, *more):
    args = ["diff-tree", "-r", "-M", "--no-color", "--no-ext-diff", "--no-textconv"]
    if part.get("within"):
        args.append("--relative=%s/" % part["within"].replace(os.sep, "/"))
    return args + list(more) + [old, part["tree"]]


def _shown(part, path):
    show = (part.get("show") or "").replace(os.sep, "/")
    return "%s/%s" % (show, path) if show else path


def _files(git, part, old, objects):
    statuses = _name_status(git(_diff(part, old, "--name-status", "-z"), objects=objects))
    counts = _numstat(git(_diff(part, old, "--numstat", "-z"), objects=objects))
    found = []
    for path, (added, removed, source) in counts.items():
        binary = added is None
        entry = {"path": _shown(part, path), "status": statuses.get(path, "modified"),
                 "added": added or 0, "removed": removed or 0, "binary": binary}
        if source:
            entry["from"] = _shown(part, source)
        found.append(entry)
    return found


def _name_status(text):
    """``--name-status -z``: ``status\\0path\\0``, a rename ``R100\\0from\\0to\\0``."""
    fields, found, i = text.split("\0"), {}, 0
    while i < len(fields) and fields[i]:
        code = fields[i][:1]
        if code in ("R", "C"):
            found[fields[i + 2]] = STATUS[code]
            i += 3
        else:
            found[fields[i + 1]] = STATUS.get(code, "modified")
            i += 2
    return found


def _numstat(text):
    """``--numstat -z``: ``added\\tremoved\\tpath\\0``, a rename
    ``added\\tremoved\\t\\0from\\0to\\0``; ``-`` counts for a binary file."""
    fields, found, i = text.split("\0"), {}, 0
    while i < len(fields):
        if not fields[i]:
            i += 1
            continue
        added, removed, path = fields[i].split("\t", 2)
        source = ""
        if path:
            i += 1
        else:
            source, path = fields[i + 1], fields[i + 2]
            i += 3
        found[path] = (None if added == "-" else int(added),
                       None if removed == "-" else int(removed), source)
    return found


def _patch(git, part, old, objects):
    show = (part.get("show") or "").replace(os.sep, "/")
    more = ["-p"]
    if show:
        more += ["--src-prefix=a/%s/" % show, "--dst-prefix=b/%s/" % show]
    return git(_diff(part, old, *more), objects=objects)


# -- kept per turn and per conversation -------------------------------------------
def begin(session_dir, turn_dir, before):
    """Publish the baseline before the agent starts changing the folder."""
    if not before.get("error"):
        _base(session_dir, before)
    path = os.path.join(turn_dir, "changes-before.json")
    _write(path + ".tmp", json.dumps(before))
    os.replace(path + ".tmp", path)


def preview(session_dir, turn_dir, folder, whole=True):
    """Compare a running turn without replacing any completed turn's patch.

    Each reader owns its temporary index; the shared git object store supports
    concurrent writers. Repository configuration is never executed.
    """
    try:
        with open(os.path.join(turn_dir, "changes-before.json"), encoding="utf-8") as handle:
            before = json.load(handle)
    except (OSError, ValueError):
        return dict(read(session_dir, None if whole else turn_dir), live=True, pending=True)
    if before.get("error"):
        raise ChangesError("The folder could not be read before the turn: %s" % before["error"])
    base = before
    if whole:
        with open(os.path.join(store(session_dir), "base.json"), encoding="utf-8") as handle:
            base = json.load(handle)
    with tempfile.TemporaryDirectory(prefix="preview-", dir=store(session_dir)) as scratch:
        after = snapshot(session_dir, folder, layout=before, scratch=scratch)
        found = compare(session_dir, base, after)
    return dict(found, live=True, kept="partial" if found["unkept"] else "all")


def keep(session_dir, turn_dir, folder, before, after, written=(), run=None):
    """Compare a turn's snapshots and keep what changed: the turn's, and the
    conversation's so far. Returns the turn's summary for its record -
    ``{"files", "added", "removed", "kept"}``, ``kept`` being ``all``,
    ``partial`` (part of the folder was too large; the files the agent wrote
    there are listed) or ``none`` (nothing was compared)."""
    turn = compare(session_dir, before, after, run)
    kept = "partial" if turn["unkept"] else "all"
    if turn["unkept"]:
        listed = _listed(folder, written, {one["path"] for one in turn["files"]})
        turn["files"] += listed
        if not any(part.get("tree") for part in (after or {}).get("parts") or []):
            kept = "none"
    _write(os.path.join(turn_dir, "changes.patch"), turn["patch"])
    _write(os.path.join(turn_dir, "changes.json"), json.dumps(dict(
        {key: value for key, value in turn.items() if key != "patch"},
        kept=kept, before=before, after=after), indent=2))
    whole = compare(session_dir, _base(session_dir, before), after, run)
    if whole["unkept"]:
        whole["files"] += _listed(folder, _written_so_far(session_dir, turn_dir, written),
                                  {one["path"] for one in whole["files"]})
    where = store(session_dir)
    _write(os.path.join(where, "session.patch"), whole["patch"])
    _write(os.path.join(where, "session.json"), json.dumps(dict(
        {key: value for key, value in whole.items() if key != "patch"},
        kept="partial" if whole["unkept"] else "all"), indent=2))
    return {"files": len(turn["files"]), "added": turn["added"],
            "removed": turn["removed"], "kept": kept}


def _base(session_dir, before):
    """The first snapshot each part had in this conversation, kept once."""
    path = os.path.join(store(session_dir), "base.json")
    try:
        with open(path, encoding="utf-8") as handle:
            base = json.load(handle)
    except (OSError, ValueError):
        base = {"parts": []}
    known = {part["key"] for part in base["parts"]}
    added = [part for part in (before or {}).get("parts") or [] if part["key"] not in known]
    if added:
        base["parts"] += added
        _write(path, json.dumps(base, indent=2))
    return base


def _written_so_far(session_dir, turn_dir, written):
    """Every file the agent wrote in this conversation: the earlier turns'
    lists, kept beside their patches, and this one's."""
    found = []
    turns = os.path.dirname(turn_dir)
    for name in sorted(os.listdir(turns)) if os.path.isdir(turns) else []:
        if os.path.join(turns, name) == turn_dir:
            continue
        try:
            with open(os.path.join(turns, name, "changes.json"), encoding="utf-8") as handle:
                found += [one["path"] for one in json.load(handle).get("files") or []
                          if one.get("status") == "written"]
        except (OSError, ValueError):
            continue
    return found + list(written or ())


def _listed(folder, written, known):
    """Files the agent wrote, as the folder shows them, not already listed."""
    found, folder = [], os.path.realpath(folder)
    for path in written or ():
        shown = os.path.relpath(path, folder) if os.path.isabs(path) else path
        shown = shown.replace(os.sep, "/")
        if shown in known or any(one["path"] == shown for one in found):
            continue
        found.append({"path": shown, "status": "written", "added": 0, "removed": 0,
                      "binary": False})
    return found


def _write(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(session_dir, turn_dir=None):
    """What was kept, a turn's (``turn_dir``) or the conversation's so far:
    ``{"files", "patch", "added", "removed", "truncated", "unkept", "kept"}``,
    empty when nothing was. Never runs git."""
    base = turn_dir or store(session_dir)
    name = "changes" if turn_dir else "session"
    try:
        with open(os.path.join(base, name + ".json"), encoding="utf-8") as handle:
            found = json.load(handle)
        with open(os.path.join(base, name + ".patch"), encoding="utf-8") as handle:
            found["patch"] = handle.read()
    except (OSError, ValueError):
        return {"files": [], "patch": "", "added": 0, "removed": 0, "truncated": False,
                "unkept": [], "kept": ""}
    found.pop("before", None)
    found.pop("after", None)
    return found
