"""``scenario.run`` - run browser scenarios as one step of a cycle.

**It spawns the launcher rather than importing the runner, and that is the
design decision in this file.** The obvious-looking alternative - call
``engine.runner.run_scenarios`` directly - does not survive contact with what it
needs. That function takes a list of *sessions*: Chrome profiles that are
already launched, seeded with a password and signed in by the per-profile
extension. Building one means the launch loop in ``session_launcher.main``, which
is not a function anybody can call, and reimplementing it here is precisely the
duplication §26 and §41 of the design document exist to prevent.

Spawning the launcher reuses all of it - profiles, Chrome, the login extension,
reports, server logs, the governor - and costs one process. It is also how this
application already works: the GUI is a client that spawns the core over the same
CLI, so a cycle step being another client of it is the pattern rather than an
exception to it.

Three things fall out of it, all good:

* **The module-global hazard disappears.** ``engine/runner.py`` keeps its stop
  flag, its stopped-session set and its report directory in module globals, so
  two concurrent in-process runs would tread on each other. Separate processes
  cannot. The concurrency group below is therefore about machine load - each of
  these opens real browsers - and not about correctness.
* **Nothing in ``cycle/`` ever holds a browser object**, so Playwright's
  greenlet-per-thread rule cannot be violated from here.
* **Stopping works.** The child is signalled the way the GUI signals it, and its
  own cooperative stop takes it from there.

What this step does *not* do is talk to the GUI on the child's behalf. A
scenario with service steps needs the control channel, and that belongs to the
process the GUI actually launched - so those steps belong in the cycle, as
``service.*`` nodes, where they read better anyway.
"""

import json
import os
import subprocess
import sys

import runtime_paths

from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output
from domain.cycle import Artifact

#: How long a signalled child is given to shut down before it is killed. Chrome
#: is given up to 15 s per window to flush its cookies, so anything shorter
#: risks closing on a login mid-write - the same reasoning the GUI's own close
#: timeout is built on.
STOP_GRACE = 25.0


def launcher_argv():
    """How to run the core from here, frozen or from a checkout.

    In a frozen build the launcher *is* the running executable, so it is invoked
    again with different flags. From a checkout it is a script beside this
    package, run with the interpreter already in use - which is the one that has
    the engine's dependencies installed.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    script = os.path.join(runtime_paths.app_root(), "session_launcher.py")
    return [sys.executable, script]


class ScenarioRun(CyclePlugin):
    """Run one or more browser scenarios, and report what they came to."""

    metadata = PluginMetadata(
        id="scenario.run",
        name="Run Scenarios",
        category=registry.SCENARIO,
        summary="Run browser scenarios against the configured sessions.",
        permissions=("process.spawn", "browser", "network",
                     "filesystem.read", "filesystem.write"),
        # Each of these opens real browser windows. Two at once is not wrong,
        # it is just more machine than most have - so they queue by default and
        # the inner --jobs is where parallelism belongs.
        concurrency_group="scenarios",
        inputs=(
            field("scenarios", "Scenarios", "text", required=True,
                  hint='Which to run: "all", a comma-separated list of ids, '
                       '"tag:smoke", or "config" to give each user its own.'),
            field("env", "Environment", "text",
                  hint="Which environment to launch, by its alias. Blank runs "
                       "every one in the configuration."),
            field("users", "Only these users", "text",
                  hint="A comma-separated list of logins. Blank runs them all."),
            # Text rather than a number, because "auto" is a value the launcher
            # takes and a number field could not express it. `problems` below
            # checks it, so the form and the validation still agree.
            field("jobs", "Windows at once", "text", default=1,
                  hint='How many browser windows may be open at once. "auto" '
                       "lets the load governor decide."),
            field("browser", "Use a browser", "check", default=True,
                  hint="Off runs service-only scenarios with no Chrome at all."),
            field("close_after", "Close the windows", "check", default=True,
                  hint="Off leaves them open for inspection, which is rarely "
                       "what a cycle wants."),
            field("config", "Configuration file", "file",
                  hint="Which users.json to read. Blank uses the default."),
            field("report_level", "Report artifacts", "text",
                  hint="What to keep: result, screen, dom, console, url. Blank "
                       "keeps the usual - everything on failure, the result on "
                       "success."),
            field("extra", "Extra flags", "args",
                  hint="Anything else to pass the launcher, for the corners "
                       "this form does not cover."),
        ),
        outputs=(
            output("exit_code", "number", "0 when every scenario passed."),
            output("passed", "number", "How many scenarios passed."),
            output("failed", "number", "How many did not."),
            output("run_dir", "string", "Where the scenario reports were written."),
            output("scenarios", "string", "What was actually run."),
        ),
    )

    def problems(self, settings):
        found = super(ScenarioRun, self).problems(settings)
        settings = settings or {}
        jobs = settings.get("jobs")
        if jobs not in (None, "") and str(jobs).lower() != "auto":
            try:
                if int(float(jobs)) < 1:
                    found.append("Windows at once has to be 1 or more, or "
                                 '"auto".')
            except (TypeError, ValueError):
                found.append('Windows at once: %r is not a number or "auto".'
                             % jobs)
        return found

    def execute(self, context, step):
        reports = context.step_dir(step.id, "reports")
        events_path = os.path.join(context.step_dir(step.id), "events.jsonl")
        argv = self._argv(context, step, reports, events_path)

        log_path = os.path.join(context.step_dir(step.id), "launcher.log")
        with open(log_path, "wb") as log_file:
            try:
                process = subprocess.Popen(
                    argv, stdout=subprocess.DEVNULL, stderr=log_file,
                    cwd=runtime_paths.app_root(),
                    env=runtime_paths.clean_subprocess_env(),
                    start_new_session=(os.name != "nt"))
            except OSError as exc:
                return registry.failed("cannot run the launcher: %s" % exc)
            code = self._wait(process, context)

        summary = _read_events(events_path)
        artifacts = [
            Artifact("jsonl", context.relative(events_path), step.id,
                     name="events", bytes=_size(events_path)),
            Artifact("log", context.relative(log_path), step.id,
                     name="launcher", bytes=_size(log_path)),
        ]
        outputs = {"exit_code": code,
                   "passed": summary["passed"],
                   "failed": summary["failed"],
                   "run_dir": context.relative(summary["run_dir"] or reports),
                   "scenarios": ",".join(summary["scenarios"])}

        if context.cancel.is_set():
            return registry.PluginResult("failed", outputs=outputs,
                                         artifacts=artifacts,
                                         message="the launcher was signalled "
                                                 "and did not finish")
        if code != 0:
            return registry.PluginResult(
                "failed", outputs=outputs, artifacts=artifacts,
                message=_why(summary, code, log_path))
        return registry.PluginResult(
            "success", outputs=outputs, artifacts=artifacts,
            message="%d scenario(s) passed" % summary["passed"])

    # -- the command line -----------------------------------------------------
    def _argv(self, context, step, reports, events_path):
        """What to run. Built from the fields, so the form is the whole surface."""
        argv = launcher_argv()
        argv.append("--run-tests=%s" % self.setting(step.settings, "scenarios"))
        argv.append("--reports-dir=%s" % reports)
        # A file rather than stdout: this process is not watching the pipe, and
        # a child blocking on a full one would hang the step.
        argv.append("--events=%s" % events_path)

        if context.flows_dir:
            argv.append("--flows-dir=%s" % context.flows_dir)
        for key, flag in (("env", "--env"), ("users", "--filter-users"),
                          ("config", "--config"),
                          ("report_level", "--report-level")):
            value = str(self.setting(step.settings, key) or "").strip()
            if value:
                argv.append("%s=%s" % (flag, value))

        jobs = self.setting(step.settings, "jobs", 1)
        if jobs not in (None, ""):
            argv.append("--jobs=%s" % jobs)
        if not self.setting(step.settings, "browser", True):
            argv.append("--no-browser")
        if self.setting(step.settings, "close_after", True):
            argv.append("--close-after")

        extra = self.setting(step.settings, "extra")
        if extra:
            argv.extend(extra if isinstance(extra, (list, tuple))
                        else _split(str(extra)))
        return argv

    def _wait(self, process, context):
        """Wait for the launcher, signalling it if the run is cancelled."""
        while True:
            try:
                return process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            if context.cancel.is_set():
                return self._stop(process)

    def _stop(self, process):
        """Ask it to stop the way the GUI does, then insist.

        SIGINT rather than SIGTERM: the launcher installs a handler for it and
        closes its windows in order, giving Chrome time to write its cookies.
        """
        _signal(process, hard=False)
        try:
            return process.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            pass
        _signal(process, hard=True)
        try:
            return process.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            return -1


def _read_events(path):
    """What the child's event stream says happened.

    Read from the file afterwards rather than followed live, because this step
    reports a result rather than narrating one - the GUI is already watching the
    cycle's own stream, and the child's is kept as an artifact for anybody who
    wants the detail.
    """
    summary = {"passed": 0, "failed": 0, "run_dir": "", "scenarios": []}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("kind")
                if kind == "run.dir":
                    summary["run_dir"] = event.get("dir") or ""
                elif kind == "flow.end":
                    if event.get("status") == "pass":
                        summary["passed"] += 1
                    else:
                        summary["failed"] += 1
                elif kind == "session.start":
                    for scenario in event.get("scenarios") or []:
                        if scenario not in summary["scenarios"]:
                            summary["scenarios"].append(scenario)
    except OSError:
        pass
    return summary


def _why(summary, code, log_path):
    """One sentence about a failed run, preferring what it actually says."""
    if summary["failed"]:
        return "%d scenario(s) failed" % summary["failed"]
    tail = _tail(log_path)
    if tail:
        return "the launcher exited %d: %s" % (code, tail)
    return "the launcher exited %d" % code


def _tail(path, limit=200):
    """The last thing the launcher said, for a message that explains itself."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return ""
    return lines[-1][:limit] if lines else ""


def _signal(process, hard):
    """Stop the launcher and anything it started, on either platform."""
    try:
        if os.name == "nt":
            process.kill() if hard else process.terminate()
            return
        import signal as signal_mod
        os.killpg(os.getpgid(process.pid),
                  signal_mod.SIGKILL if hard else signal_mod.SIGINT)
    except (OSError, ProcessLookupError, PermissionError):
        try:
            process.kill() if hard else process.terminate()
        except OSError:
            pass


def _split(text):
    import shlex
    try:
        return shlex.split(text)
    except ValueError:
        return []


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
