"""One step's plugin, run as a program of its own inside the step's restrictions.

Jira and Teams steps run inside QAVector, where no sandbox reaches. When such a
step is restricted, the executor starts this instead - ``qavector-core
--cycle-step-worker``, through :func:`cycle.restrict.popen`, so inside the step's
sandbox - and hands it the step on stdin: the plugin, its settings with secrets
already filled in (on a pipe, never on disk or a command line), and the run's
folder. The worker runs the same plugin and writes the result back as JSON on
stdout. Whatever the plugin prints or logs goes to the step's ``worker.log``.

The worker sees only what the step may: the network if it is on, the run's
folder, the project folder as Files says. A plugin that needs more fails the
way it would anywhere it was not allowed.
"""

import dataclasses
import json
import os
import subprocess
import sys
import threading

FLAG = "--cycle-step-worker"
#: How long a stopped worker gets to go before it is killed.
STOP_GRACE = 5.0


# -- the executor's side ------------------------------------------------------
def call(context, step):
    """Run ``step``'s plugin in a worker inside the current restriction."""
    import runtime_paths
    from cycle import restrict
    from cycle.plugins.scenario import launcher_argv
    step_dir = context.step_dir(step.id)
    request = json.dumps({"plugin": step.plugin, "workspace": context.workspace,
                          "step": {"id": step.id, "plugin": step.plugin, "label": step.label or "",
                                   "settings": step.settings}}).encode("utf-8")
    # From a checkout the worker is this source tree, which may sit in a
    # hidden home folder; an installed core is under /opt, which is visible.
    code = [] if runtime_paths.FROZEN else [
        {"path": runtime_paths.app_root(), "access": "read_only", "reason": "QAVector's own code"}]
    with open(os.path.join(step_dir, "worker.log"), "wb") as log:
        process = restrict.popen(launcher_argv() + [FLAG], cwd=step_dir, folders=code,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
    answer = []
    reader = threading.Thread(target=lambda: answer.append(process.stdout.read()), daemon=True)
    reader.start()
    try:
        process.stdin.write(request)
        process.stdin.close()
    except OSError:
        pass                    # it has gone already; its exit says why
    while process.poll() is None:
        if context.cancel.wait(0.1):
            _stop(process)
            reader.join(STOP_GRACE)
            context.cancel.raise_if_set()
    reader.join()
    return _result(b"".join(answer), process.returncode, os.path.join(step_dir, "worker.log"))


def _stop(process):
    process.terminate()
    try:
        process.wait(STOP_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _result(raw, code, log_path):
    from cycle.registry import PluginResult
    from domain.cycle import Artifact
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise RuntimeError("the step's worker exited %s without a result%s" % (code, _tail(log_path)))
    if "error" in document:
        # Raised under the plugin's own exception name, so the step reads the
        # same as it would have without restrictions: "JiraError: ...".
        name = str(document.get("type") or "Error")
        raise type(name if name.isidentifier() else "Error", (Exception,), {})(document["error"])
    result = dict(document["result"])
    result["artifacts"] = [Artifact(**one) for one in result.get("artifacts") or []]
    return PluginResult(**result)


def _tail(log_path, most=600):
    try:
        with open(log_path, "rb") as handle:
            text = handle.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return (": " + text[-most:]) if text else ""


# -- the worker's side ----------------------------------------------------------
class _Context:
    """What a Jira or Teams step uses of the run: its folder, its step folder,
    and a cancel token. Stopping is the executor's: it ends this process."""

    def __init__(self, workspace):
        from cycle.context import CancelToken
        self.workspace = workspace
        self.cancel = CancelToken()

    def step_dir(self, step_id, *parts):
        path = os.path.join(self.workspace, "steps", step_id, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def artifacts_dir(self):
        path = os.path.join(self.workspace, "artifacts")
        os.makedirs(path, exist_ok=True)
        return path

    def relative(self, path):
        try:
            relative = os.path.relpath(os.path.abspath(path), self.workspace)
        except ValueError:
            return path
        return path if relative.startswith(os.pardir) else relative


def main():
    """Read one step on stdin, run it, write its result on stdout."""
    answer_to = sys.stdout
    # Anything the plugin prints is for the log, not the answer.
    sys.stdout = sys.stderr
    try:
        request = json.load(sys.stdin)
        from cycle import registry
        from domain.cycle import CycleStep
        document = request["step"]
        step = CycleStep(document["id"], document["plugin"], label=document.get("label") or "",
                         settings=document.get("settings") or {})
        result = registry.get(request["plugin"]).execute(_Context(request["workspace"]), step)
        answer = {"result": dataclasses.asdict(result)}
    except Exception as exc:              # noqa: BLE001 - the executor reports it
        answer = {"error": str(exc), "type": type(exc).__name__}
    answer_to.write(json.dumps(answer, default=str))
    answer_to.flush()
    return 0
