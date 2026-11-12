"""The skills library: folders of instructions an agent can be handed.

A *skill* is a folder with a ``SKILL.md`` in it - what the skill is for, and how
to do it - and whatever that file refers to: notes, templates, scripts. The
library is QAVector's, like the agent connections, and the same in every
project::

    <user data>/agent-skills/
      odoo-review/            the skill, exactly as it was looked at
      odoo-review.source.json where it came from: a folder, or a repository,
                              a branch and the commit it was taken at
      .pending/<token>/       what somebody is looking at and has not added

**A skill is not trusted because it was downloaded.** Its instructions are read
by an agent and its scripts may be run by one, so nothing arrives in the
library unseen: :func:`look` fetches into ``.pending`` and says what is there -
every file, where it came from, at which commit - and :func:`add` moves in
exactly that copy, and only when asked to. What was looked at is what is added;
there is no second download in between for it to differ in.

**Nothing updates by itself.** A skill stays at the commit it was added at.
:func:`look_again` fetches its source once more, because somebody asked, and
says what changed; adding that is the same deliberate step.

Fetching is ``git`` and nothing else: any repository git can clone, with the
person's own credentials, and no token of theirs passing through here.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time

import runtime_paths

DIR_NAME = "agent-skills"
SKILL_FILE = "SKILL.md"
PENDING = ".pending"
LOOK_FILE = "look.json"

#: What a skill may be called: what the agents themselves accept, and safe as
#: a folder name.
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TOKEN = re.compile(r"^[0-9a-f]{16}$")

#: A skill is instructions and a few files. Anything far past that is not one,
#: and is not copied into the library to find out.
MAX_FILES = 500
MAX_BYTES = 20 * 1024 * 1024
#: How many skills one look may find, and how deep it searches for them.
MAX_CANDIDATES = 100
MAX_DEPTH = 6
#: How long something looked at and not added is kept.
PENDING_SECONDS = 3600
#: How long a fetch may take.
FETCH_TIMEOUT = 180

#: Files an agent could run rather than read. A skill that has any says so,
#: because whether they can run depends on what a turn lets the agent do.
SCRIPTS = (".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".rb", ".pl", ".php",
           ".ps1", ".bat", ".cmd")

_SKIPPED = (".git", "node_modules", "__pycache__")
_SCP = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./~-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class SkillError(Exception):
    """A skill that cannot be read, fetched or added; ``kind`` says which."""

    def __init__(self, message, kind="invalid"):
        super().__init__(message)
        self.kind = kind


# -- where --------------------------------------------------------------------
def default_root():
    return os.path.join(runtime_paths.user_data_root(), DIR_NAME)


def resolve_root(configured=""):
    configured = (configured or "").strip()
    return os.path.abspath(os.path.expanduser(configured)) if configured else default_root()


def _within(parent, child):
    parent, child = os.path.realpath(parent), os.path.realpath(child)
    return child == parent or child.startswith(parent + os.sep)


# -- one skill ------------------------------------------------------------------
def _front_matter(text):
    """The ``name`` and ``description`` a ``SKILL.md`` opens with, as a dict.

    Read as data and never run. Written as YAML between two ``---`` lines; a
    file without that opening says nothing about itself, which is allowed.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    try:
        import yaml
        found = yaml.safe_load(block)
        return found if isinstance(found, dict) else {}
    except ImportError:
        pass
    except Exception:                    # noqa: BLE001 - not YAML: says nothing
        return {}
    found = {}
    for line in block.splitlines():
        key, colon, value = line.partition(":")
        if colon and key.strip() and not key.startswith((" ", "\t", "#")):
            found[key.strip()] = value.strip().strip("'\"")
    return found


def describe(folder):
    """What one skill folder holds: ``{name, description, files, bytes, scripts}``.

    Raises :class:`SkillError` for a folder that is not a skill, is too big to
    be one, or reaches outside itself through a link.
    """
    folder = os.path.abspath(folder)
    skill = os.path.join(folder, SKILL_FILE)
    if not os.path.isfile(skill):
        raise SkillError("There is no %s in %s." % (SKILL_FILE, folder))
    files, total, scripts = [], 0, False
    for base, directories, names in os.walk(folder):
        directories[:] = sorted(one for one in directories if one not in _SKIPPED)
        for name in sorted(names) + list(directories):
            path = os.path.join(base, name)
            # A link is followed when the skill is copied, so one that points
            # outside would bring in a file nobody was shown.
            if os.path.islink(path) and not _within(folder, path):
                raise SkillError("%s is a link to somewhere outside the skill."
                                 % os.path.relpath(path, folder))
        for name in sorted(names):
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                continue
            total += os.path.getsize(path)
            files.append(os.path.relpath(path, folder).replace(os.sep, "/"))
            scripts = scripts or name.lower().endswith(SCRIPTS) or (
                os.name != "nt" and os.access(path, os.X_OK))
            if len(files) > MAX_FILES or total > MAX_BYTES:
                raise SkillError("This is too big to be a skill: more than %d files or %d MB."
                                 % (MAX_FILES, MAX_BYTES // (1024 * 1024)))
    try:
        with open(skill, encoding="utf-8", errors="replace") as handle:
            front = _front_matter(handle.read(64 * 1024))
    except OSError as exc:
        raise SkillError("cannot read %s: %s" % (skill, exc))
    name = str(front.get("name") or os.path.basename(folder)).strip().lower()
    if not NAME.match(name):
        # Named by its folder when what it calls itself cannot be a name.
        name = re.sub(r"[^a-z0-9]+", "-", os.path.basename(folder).lower()).strip("-")[:64]
    if not NAME.match(name):
        raise SkillError("The skill has no usable name: lowercase letters, digits and "
                         "dashes, in its SKILL.md or as its folder.")
    return {"name": name,
            "description": " ".join(str(front.get("description") or "").split())[:1024],
            "files": sorted(files), "bytes": total, "scripts": scripts}


def find(folder):
    """Every skill under ``folder``, as paths relative to it ("" for itself)."""
    folder = os.path.abspath(folder)
    if os.path.isfile(os.path.join(folder, SKILL_FILE)):
        return [""]
    found = []
    depth = folder.rstrip(os.sep).count(os.sep)
    for base, directories, names in os.walk(folder):
        directories[:] = sorted(one for one in directories
                                if one not in _SKIPPED and not one.startswith(".")
                                and not os.path.islink(os.path.join(base, one)))
        if SKILL_FILE in names:
            found.append(os.path.relpath(base, folder).replace(os.sep, "/"))
            directories[:] = []         # a skill's own folders are part of it
        elif base.count(os.sep) - depth >= MAX_DEPTH:
            directories[:] = []
        if len(found) >= MAX_CANDIDATES:
            break
    return found


def _digests(folder):
    """``{relative path: digest}`` of every file of a skill."""
    found = {}
    for base, directories, names in os.walk(folder):
        directories[:] = [one for one in directories if one not in _SKIPPED]
        for name in names:
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                continue
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            found[os.path.relpath(path, folder).replace(os.sep, "/")] = digest.hexdigest()
    return found


# -- the library ----------------------------------------------------------------
def _source_path(root, name):
    return os.path.join(resolve_root(root), "%s.source.json" % name)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def folder_of(root, name):
    """Where one skill of the library is. Raises for a name that is not one."""
    if not isinstance(name, str) or not NAME.match(name):
        raise SkillError("%r is not a skill's name." % (name,))
    return os.path.join(resolve_root(root), name)


def listing(root=None):
    """Every skill in the library, by name, with where each came from.

    One that can no longer be read - somebody edited the folder by hand - is
    still listed, with what is wrong, so it can be removed.
    """
    base = resolve_root(root)
    try:
        names = sorted(name for name in os.listdir(base)
                       if NAME.match(name) and os.path.isdir(os.path.join(base, name)))
    except OSError:
        return []
    found = []
    for name in names:
        source = _read_json(_source_path(root, name))
        try:
            info = describe(os.path.join(base, name))
            problem = ""
        except SkillError as exc:
            info, problem = {"description": "", "files": [], "bytes": 0, "scripts": False}, str(exc)
        found.append(dict(info, name=name, source=source, problem=problem))
    return found


def resolve(root, names):
    """``[(name, folder)]`` for the skills a turn was asked to take. Raises
    :class:`SkillError` naming the first that is not in the library."""
    found = []
    for name in names or []:
        folder = folder_of(root, name)
        if not os.path.isfile(os.path.join(folder, SKILL_FILE)):
            raise SkillError("The skill %r is not in the library any more." % name, "not_found")
        found.append((name, folder))
    return found


def remove(root, name):
    """Take a skill out of the library. Returns whether there was one."""
    folder = folder_of(root, name)
    if not os.path.isdir(folder):
        return False
    shutil.rmtree(folder, ignore_errors=True)
    try:
        os.unlink(_source_path(root, name))
    except OSError:
        pass
    return True


# -- fetching -------------------------------------------------------------------
def _git(arguments, cwd=None):
    """Run git, never asking anybody anything. Returns its stdout."""
    environment = runtime_paths.clean_subprocess_env()
    # A fetch that stops to ask for a password waits for ever: nobody is at a
    # terminal. The person's credential helper and ssh agent still answer.
    environment.update(GIT_TERMINAL_PROMPT="0",
                       GIT_SSH_COMMAND=environment.get("GIT_SSH_COMMAND")
                       or "ssh -oBatchMode=yes")
    try:
        done = subprocess.run(
            # Never a transport that runs a program: ext:: is a command line.
            ["git", "-c", "protocol.ext.allow=never"] + list(arguments),
            cwd=cwd, env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=FETCH_TIMEOUT)
    except FileNotFoundError:
        raise SkillError("git is not installed; a skill is fetched with it.", "unavailable")
    except subprocess.TimeoutExpired:
        raise SkillError("The repository did not answer in %d seconds." % FETCH_TIMEOUT,
                         "unreachable")
    if done.returncode != 0:
        said = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SkillError("git could not fetch it: %s" % (said[-1] if said else "no reason given"),
                         "unreachable")
    return (done.stdout or b"").decode("utf-8", "replace")


def clean_url(url):
    """``url`` without a name or a token written into it - what is kept and shown."""
    return re.sub(r"^(https?://)[^/@]+@", r"\1", str(url or "").strip())


def parse_link(link):
    """``{"url", "rest"}`` from what somebody pasted.

    A repository's address is taken as it is. A page of one - GitHub's or
    GitLab's ``.../tree/<branch>/<path>`` - is split into the repository and
    the rest, which :func:`split_rest` then divides into branch and path.
    Raises for anything git should not be handed.
    """
    link = str(link or "").strip()
    if not link:
        raise SkillError("Give the address of a repository, or of a folder in one.")
    if link.startswith("-") or any(char in link for char in "\n\r\0"):
        raise SkillError("That is not the address of a repository.")
    if link.startswith("http://"):
        raise SkillError("Use an https:// address: over http anybody on the way could "
                         "change what is fetched.")
    if not (link.startswith(("https://", "ssh://", "file://")) or _SCP.match(link)
            or os.path.isabs(link)):
        raise SkillError("That is not an address git can fetch: use https://, ssh:// or "
                         "git@host:path.")
    if link.startswith("https://"):
        for marker in ("/-/tree/", "/tree/", "/-/blob/", "/blob/"):
            if marker in link:
                url, rest = link.split(marker, 1)
                rest = rest.split("?", 1)[0].split("#", 1)[0].strip("/")
                if "blob" in marker:
                    rest = rest.rsplit("/", 1)[0] if "/" in rest else rest
                return {"url": url, "rest": rest}
    return {"url": link, "rest": ""}


def split_rest(rest, refs):
    """``(ref, path)`` from what follows ``/tree/`` in a page's address.

    A branch may have slashes in its name, so the address alone cannot say
    where the branch ends: ``refs`` - the repository's own branches and tags -
    decides, the longest that fits winning. With none that fits the first part
    is taken as the ref, which is right for a commit.
    """
    rest = str(rest or "").strip("/")
    if not rest:
        return "", ""
    fitting = [ref for ref in refs or [] if rest == ref or rest.startswith(ref + "/")]
    if fitting:
        ref = max(fitting, key=len)
        return ref, rest[len(ref):].strip("/")
    ref, _slash, path = rest.partition("/")
    return ref, path


def _refs(url):
    listed = _git(["ls-remote", "--heads", "--tags", "--", url])
    found = []
    for line in listed.splitlines():
        _sha, _tab, name = line.partition("\t")
        for prefix in ("refs/heads/", "refs/tags/"):
            if name.startswith(prefix) and not name.endswith("^{}"):
                found.append(name[len(prefix):])
    return found


def _checkout(url, ref, where):
    """Fetch ``url`` at ``ref`` into ``where``. Returns ``(ref, commit)``."""
    if _SHA.match(ref or ""):
        _git(["init", "--quiet", where])
        _git(["fetch", "--quiet", "--depth", "1", "--", url, ref], cwd=where)
        _git(["checkout", "--quiet", "FETCH_HEAD"], cwd=where)
    else:
        _git(["clone", "--quiet", "--depth", "1", "--single-branch", "--no-recurse-submodules"]
             + (["--branch", ref] if ref else []) + ["--", url, where])
        if not ref:
            ref = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=where).strip()
    return ref, _git(["rev-parse", "HEAD"], cwd=where).strip()


# -- looking, and adding what was looked at ---------------------------------------
def _pending(root):
    return os.path.join(resolve_root(root), PENDING)


def _sweep(root):
    """Forget what was looked at long ago and never added."""
    base = _pending(root)
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        path = os.path.join(base, name)
        try:
            if time.time() - os.path.getmtime(path) > PENDING_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def look(root, request):
    """Fetch what ``request`` names and say what is there. Adds nothing.

    ``request`` is ``{"folder": path}`` or ``{"url": address}`` - with ``ref``
    and ``path`` when the address is a repository's and those are known.
    Returns ``{token, source, candidates}``: each candidate a skill found, as
    :func:`describe` says it, with ``pick`` (where it is under the source),
    ``installed`` (whether the library has one of that name) and ``problems``.
    The token names this look to :func:`add` for the next hour.
    """
    request = request or {}
    _sweep(root)
    os.makedirs(_pending(root), mode=0o700, exist_ok=True)
    scratch = None
    try:
        if request.get("folder"):
            base = os.path.abspath(os.path.expanduser(str(request["folder"]).strip()))
            if not os.path.isdir(base):
                raise SkillError("%s is not a folder." % base)
            source = {"kind": "folder", "folder": base}
        else:
            link = parse_link(request.get("url"))
            url = link["url"]
            if "ref" in request or "path" in request:
                ref, path = str(request.get("ref") or ""), str(request.get("path") or "")
            elif link["rest"]:
                ref, path = split_rest(link["rest"], _refs(url))
            else:
                ref, path = "", ""
            scratch = tempfile.mkdtemp(prefix="qavector-skill-")
            clone = os.path.join(scratch, "repository")
            ref, commit = _checkout(url, ref, clone)
            base = os.path.normpath(os.path.join(clone, path))
            if not _within(clone, base) or not os.path.isdir(base):
                raise SkillError("There is no folder %r in that repository at %s."
                                 % (path, ref), "not_found")
            source = {"kind": "git", "url": clean_url(url), "ref": ref, "commit": commit,
                      "path": path.strip("/")}
        picks = find(base)
        if not picks:
            raise SkillError("No %s was found there: it is not a skill, and holds none."
                             % SKILL_FILE, "not_found")
        token = secrets.token_hex(8)
        stage = os.path.join(_pending(root), token)
        os.makedirs(stage, mode=0o700)
        candidates = []
        for index, pick in enumerate(picks):
            where = os.path.join(base, pick) if pick else base
            entry = {"pick": pick, "staged": "", "problems": [],
                     "name": os.path.basename(where.rstrip(os.sep))}
            try:
                entry.update(describe(where))
                shutil.copytree(where, os.path.join(stage, str(index)), symlinks=False,
                                ignore=shutil.ignore_patterns(*_SKIPPED))
                entry["staged"] = str(index)
                entry["installed"] = os.path.isdir(os.path.join(resolve_root(root), entry["name"]))
            except (SkillError, OSError, shutil.Error) as exc:
                entry["problems"].append(str(exc))
            candidates.append(entry)
        found = {"token": token, "source": source, "candidates": candidates}
        with open(os.path.join(stage, LOOK_FILE), "w", encoding="utf-8") as handle:
            json.dump(found, handle)
        return found
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def add(root, token, picks, replace=False):
    """Put skills that were looked at into the library. Returns what was added.

    ``picks`` are the ``pick`` of each chosen candidate of the look ``token``
    names. Everything is checked before anything is moved: a skill whose name
    the library already has is refused unless ``replace`` says to take its
    place, and then none of the picks is added.
    """
    if not isinstance(token, str) or not TOKEN.match(token):
        raise SkillError("That is not something that was looked at.", "expired")
    stage = os.path.join(_pending(root), token)
    looked = _read_json(os.path.join(stage, LOOK_FILE))
    if not looked:
        raise SkillError("What was looked at is gone - it is kept for an hour. Look again.",
                         "expired")
    chosen = []
    for pick in picks or []:
        candidate = next((one for one in looked.get("candidates") or []
                          if one.get("pick") == pick), None)
        if candidate is None or not candidate.get("staged") or candidate.get("problems"):
            raise SkillError("%r is not a skill that look found." % (pick,))
        if os.path.isdir(folder_of(root, candidate["name"])) and not replace:
            raise SkillError("A skill called %r is already in the library."
                             % candidate["name"], "exists")
        chosen.append(candidate)
    if not chosen:
        raise SkillError("Choose which skill to add.")
    if len({one["name"] for one in chosen}) != len(chosen):
        raise SkillError("Two of those are called the same; add one of them.")
    added = []
    source = looked.get("source") or {}
    for candidate in chosen:
        name = candidate["name"]
        destination = folder_of(root, name)
        aside = "%s.replaced-%s" % (destination, token)
        if os.path.isdir(destination):
            os.rename(destination, aside)
        os.rename(os.path.join(stage, candidate["staged"]), destination)
        shutil.rmtree(aside, ignore_errors=True)
        where = dict(source, added=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        if source.get("kind") == "git":
            where["path"] = "/".join(part for part in (source.get("path"), candidate["pick"])
                                     if part)
        else:
            where["folder"] = os.path.normpath(os.path.join(source.get("folder") or "",
                                                            candidate["pick"]))
        with open(_source_path(root, name), "w", encoding="utf-8") as handle:
            json.dump(where, handle, indent=2)
            handle.write("\n")
        added.append(dict({key: candidate[key] for key in
                           ("name", "description", "files", "bytes", "scripts")},
                          source=where, problem=""))
    shutil.rmtree(stage, ignore_errors=True)
    return added


# -- export and import -----------------------------------------------------------
def export(root, names):
    """The library's skills ``names``, with every file, for an export file.

    ``[{name, description, source, files: [{path, data, executable}]}]`` -
    ``data`` base64, so a script travels byte for byte.
    """
    import base64
    found = []
    for name in names or []:
        folder = folder_of(root, name)
        info = describe(folder)
        files = []
        for relative in info["files"]:
            path = os.path.join(folder, relative)
            with open(path, "rb") as handle:
                data = base64.b64encode(handle.read()).decode("ascii")
            files.append({"path": relative, "data": data,
                          "executable": os.name != "nt" and os.access(path, os.X_OK)})
        found.append({"name": name, "description": info["description"],
                      "source": _read_json(_source_path(root, name)), "files": files})
    return found


def _safe_relative(path):
    """``path`` as a place inside a skill's folder, or raises. A file of an
    export is written where it says, so it must not be able to say anywhere
    else."""
    path = str(path or "").replace("\\", "/")
    parts = path.split("/")
    if (not path or path.startswith("/") or ":" in parts[0]
            or any(part in ("", ".", "..") for part in parts)):
        raise SkillError("%r is not a place inside a skill." % path)
    return os.path.join(*parts)


def import_skills(root, entries, mode="merge"):
    """Put the skills of an export file into the library. Returns what was done, said.

    Each is written into a folder of its own, checked as a fetched skill is -
    a ``SKILL.md``, no more than a skill holds - and only then moved in. A name
    the library has already is left alone (``merge``) or added beside it as
    ``<name>-copy`` (``copy``).
    """
    import base64
    said = []
    base = resolve_root(root)
    os.makedirs(os.path.join(base, PENDING), mode=0o700, exist_ok=True)
    for entry in entries or []:
        name = str((entry or {}).get("name") or "")
        try:
            folder_of(root, name)
            target = name
            if os.path.isdir(folder_of(root, name)):
                if mode != "copy":
                    said.append("Skill %s is in the library already; left as it is." % name)
                    continue
                target = _copy_name(root, name)
            stage = tempfile.mkdtemp(prefix="import-", dir=os.path.join(base, PENDING))
            try:
                files, total = entry.get("files") or [], 0
                if not isinstance(files, list) or len(files) > MAX_FILES:
                    raise SkillError("Skill %s holds more than a skill does." % name)
                for one in files:
                    relative = _safe_relative((one or {}).get("path"))
                    data = base64.b64decode(str(one.get("data") or ""), validate=True)
                    total += len(data)
                    if total > MAX_BYTES:
                        raise SkillError("Skill %s is too big to be a skill." % name)
                    path = os.path.join(stage, relative)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                         0o755 if one.get("executable") else 0o644)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                describe(stage)
                os.rename(stage, folder_of(root, target))
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise
            source = dict(entry.get("source") or {}) if isinstance(entry.get("source"), dict) \
                else {}
            source["imported"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            with open(_source_path(root, target), "w", encoding="utf-8") as handle:
                json.dump(source, handle, indent=2)
                handle.write("\n")
            said.append("Skill %s added%s." % (target, "" if target == name
                                                 else " beside %s" % name))
        except (SkillError, ValueError, OSError) as exc:
            said.append("Skill %s was not imported: %s" % (name or "?", exc))
    return said


def _copy_name(root, name):
    stem = name[:59]
    candidate, number = "%s-copy" % stem, 2
    while os.path.isdir(folder_of(root, candidate)):
        candidate = "%s-copy%d" % (stem, number)
        number += 1
    return candidate


def look_again(root, name):
    """Fetch a library skill's source once more and say what changed.

    Returns what :func:`look` returns, with ``name`` and ``changes``:
    ``{"added", "removed", "changed"}`` file lists against the library's copy,
    and ``was`` / ``now`` - the commit it was added at, and the one found.
    Nothing in the library changes; :func:`add` with ``replace`` does that.
    """
    folder = folder_of(root, name)
    source = _read_json(_source_path(root, name))
    if not os.path.isdir(folder) or not source:
        raise SkillError("The library does not know where %r came from." % name, "not_found")
    if source.get("kind") == "git":
        request = {"url": source.get("url"), "ref": source.get("ref") or "",
                   "path": source.get("path") or ""}
    else:
        request = {"folder": source.get("folder")}
    looked = look(root, request)
    candidate = next((one for one in looked["candidates"] if one.get("pick") == ""), None)
    if candidate is None or candidate.get("problems"):
        shutil.rmtree(os.path.join(_pending(root), looked["token"]), ignore_errors=True)
        raise SkillError((candidate or {}).get("problems", ["It is not a skill any more."])[0]
                         if candidate else "There is no skill at its source any more.",
                         "not_found")
    # Whatever it calls itself now, it takes the place of the skill it was
    # fetched for: conversations name a library skill by the name it has here.
    candidate["name"] = name
    looked["candidates"] = [candidate]
    with open(os.path.join(_pending(root), looked["token"], LOOK_FILE), "w",
              encoding="utf-8") as handle:
        json.dump(looked, handle)
    before = _digests(folder)
    after = _digests(os.path.join(_pending(root), looked["token"], candidate["staged"]))
    changes = {"added": sorted(set(after) - set(before)),
               "removed": sorted(set(before) - set(after)),
               "changed": sorted(path for path in set(before) & set(after)
                                 if before[path] != after[path])}
    return dict(looked, name=name, candidates=[candidate], changes=changes,
                was=source.get("commit") or "", now=looked["source"].get("commit") or "")
