"""Restrictions on a cycle's steps, and the one place their processes start.

A cycle that names a ``project_folder`` may carry ``restrictions`` (see
``execution_runtime/restrictions.py``); a step may only narrow them. When the
executor starts a step it works out what applies and marks the worker thread
with it. Every place a plugin starts a process - the shell command, git, an
agent's CLI - goes through :func:`popen` or :func:`run` here, which start it
inside the sandbox when there is one and exactly as before when there is not.

**System steps** never get restrictions. Some because they start nothing on the
project folder - an approval, a report, a Jira call happens inside QAVector
itself - and one because it cannot be held to a folder yet: ``scenario.run``
drives a real browser. Any other step can be marked ``system: true`` on purpose,
and the canvas says so on the node; nothing runs unrestricted without showing it.

A cycle with no project folder is unaffected: it runs as it always has.
"""

import os
import subprocess
import threading
from contextlib import contextmanager

#: Plugins whose processes run inside the restrictions. Everything else is a
#: system step.
RESTRICTABLE = frozenset((
    "command.shell", "git.checkout", "git.commit", "git.prepare_branch",
    "agent.review", "agent.edit", "agent.implement", "agent.merge", "task.brief",
))

_local = threading.local()


def is_system(step):
    return bool(getattr(step, "system", False)) or step.plugin not in RESTRICTABLE


def problems(cycle):
    """What is wrong with a cycle's restrictions, as readable lines."""
    from execution_runtime.errors import RuntimePolicyError
    from execution_runtime import restrictions
    found = []
    folder = getattr(cycle, "project_folder", "") or ""
    if not isinstance(folder, str):
        return ["project_folder must be a path"]
    if cycle.restrictions and not folder:
        found.append("restrictions need a project_folder: they are set on that folder.")
    if (folder or cycle.restrictions) and isinstance(cycle.execution, dict) and "runtime" in cycle.execution:
        found.append("Use either restrictions or execution.runtime, not both.")
    try:
        restrictions.parse(cycle.restrictions, "restrictions")
    except RuntimePolicyError as exc:
        found.append("%s: %s" % (exc.field or "restrictions", exc))
    for step in cycle.steps:
        if not isinstance(step.system, bool):
            found.append("%s: system must be true or false" % step.id)
        if step.restrictions and not folder:
            found.append("%s: restrictions need the cycle's project_folder." % step.id)
        elif step.restrictions and is_system(step):
            found.append("%s: a system step runs without restrictions; remove them or the system mark." % step.id)
        try:
            restrictions.parse(step.restrictions, "%s.restrictions" % step.id)
        except RuntimePolicyError as exc:
            found.append("%s: %s" % (exc.field or step.id, exc))
    return found


def project_folder(cycle, scope):
    """The cycle's project folder with ``${vars...}`` filled in, or ""."""
    folder = (getattr(cycle, "project_folder", "") or "").strip()
    if not folder:
        return ""
    from cycle import variables
    resolved = str(variables.resolve(folder, scope) or "").strip()
    if not resolved:
        return ""
    return os.path.abspath(os.path.expanduser(resolved))


class StepRestriction:
    """What one step's processes are held to, and how to start one of them."""

    def __init__(self, effective, step_dir):
        self.effective = effective
        self.step_dir = step_dir

    @property
    def folder(self):
        return self.effective["project_folder"]

    def workdir(self, written):
        """Where a command runs: the project folder unless the step says.

        A relative ``dir`` is read against the project folder, not the run's
        workspace - the run's workspace is not visible to a restricted step.
        """
        written = os.path.expanduser(str(written or "").strip())
        if not written:
            return self.folder
        return written if os.path.isabs(written) else os.path.join(self.folder, written)

    def prepare(self, argv, cwd, env):
        from execution_runtime import toolfolders
        from execution_runtime.backends.linux.restricted import launcher
        visible = []
        if self.effective["files"] in ("read_write", "read_only"):
            visible.append(self.folder)
        visible += [one["path"] for one in self.effective["folders"]]
        argv, tools = toolfolders.for_command(argv, env, visible=visible)
        if self.effective["files"] in ("all", "none"):
            tools = []
        extra = list(tools)
        if self.effective["files"] != "all" and self.step_dir:
            # Its own folder for what it writes; the rest of the run is not its business.
            extra.append({"path": self.step_dir, "access": "read_write", "reason": "this step's own output folder"})
        return launcher().prepare(self.effective, argv, cwd or self.folder, env, extra)


def for_step(cycle, step, scope, step_dir):
    """The restriction for ``step``, or None when it runs as it always has."""
    folder = project_folder(cycle, scope)
    if not folder or is_system(step):
        return None
    from execution_runtime import restrictions
    effective = restrictions.resolve([("cycle", cycle.restrictions), ("step", step.restrictions)], folder)
    if restrictions.unrestricted(effective):
        return None
    return StepRestriction(effective, step_dir)


@contextmanager
def active(restriction):
    """Mark this thread as running a step held to ``restriction``."""
    previous = getattr(_local, "restriction", None)
    _local.restriction = restriction
    try:
        yield
    finally:
        _local.restriction = previous


def enter(restriction):
    """:func:`active` for code that cannot use a ``with`` block. Pass what it
    returns to :func:`leave`."""
    previous = getattr(_local, "restriction", None)
    _local.restriction = restriction
    return previous


def leave(previous):
    _local.restriction = previous


def current():
    return getattr(_local, "restriction", None)


def _argv(command, shell):
    if shell:
        if not isinstance(command, str):
            command = subprocess.list2cmdline(command)
        return ["/bin/sh", "-c", command]
    return [command] if isinstance(command, str) else list(command)


def popen(command, cwd=None, env=None, shell=False, **kwargs):
    """``subprocess.Popen`` for a step: inside its restrictions when it has any."""
    restriction = current()
    if restriction is None:
        return subprocess.Popen(command, cwd=cwd, env=env, shell=shell, **kwargs)
    prepared = restriction.prepare(_argv(command, shell), cwd, dict(os.environ if env is None else env))
    try:
        return subprocess.Popen(prepared.argv, cwd=prepared.cwd, env=prepared.env,
                                pass_fds=prepared.pass_fds, **kwargs)
    finally:
        prepared.close()


def run(command, cwd=None, env=None, shell=False, **kwargs):
    """``subprocess.run`` for a step: inside its restrictions when it has any."""
    restriction = current()
    if restriction is None:
        return subprocess.run(command, cwd=cwd, env=env, shell=shell, **kwargs)
    prepared = restriction.prepare(_argv(command, shell), cwd, dict(os.environ if env is None else env))
    try:
        return subprocess.run(prepared.argv, cwd=prepared.cwd, env=prepared.env,
                              pass_fds=prepared.pass_fds, **kwargs)
    finally:
        prepared.close()
