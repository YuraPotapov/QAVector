"""``command.shell`` - run a command, stream what it says, report how it went.

The plainest plugin there is, and the one every other is measured against. It is
also the one that establishes three habits the rest follow:

* **Output is streamed, in batches.** A build that prints ten thousand lines
  must not become ten thousand events: ``engine/events.py`` holds one lock and
  flushes every line, so a chatty step would serialise every other step's
  progress behind it. Lines are gathered and sent as one event on a short tick,
  the same arrangement the launcher already uses for backend log lines.
* **The deadline is passed down, not watched.** The executor sets the step's
  cancel token when the timeout expires, but a token cannot interrupt a blocked
  ``read``. So the wait is bounded and the token checked between waits, and when
  the run is stopping the child is signalled and then killed rather than left
  to finish in the background.
* **What it produces is declared.** ``exit_code``, ``stdout_tail`` and the paths
  of the two log files are in the metadata, so a later step can be written
  against them before this one has ever run.
"""

import logging
import os
import subprocess
import threading
import time

from cycle import registry
from cycle.registry import CyclePlugin, PluginMetadata, field, output
from domain.cycle import Artifact

log = logging.getLogger("cycle.plugins.command")

#: How long output is gathered before it is sent on. Short enough that a run
#: reads as live, long enough that a noisy command does not flood the stream.
FLUSH_SECONDS = 0.2

#: How many lines of the tail an output carries. A later step or a report wants
#: the end of a failed build, not the whole of it - the whole of it is on disk.
TAIL_LINES = 40

#: How long a signalled child is given to exit before it is killed. The same
#: eight seconds the service supervisor allows, for the same reason: long
#: enough for anything with a shutdown path, short enough to not look hung.
STOP_GRACE = 8.0


class ShellCommand(CyclePlugin):
    """Run one command line in the run's workspace."""

    metadata = PluginMetadata(
        id="command.shell",
        name="Shell Command",
        category=registry.ACTION,
        summary="Run a command and collect its output.",
        permissions=("process.spawn", "filesystem.read", "filesystem.write"),
        inputs=(
            field("command", "Command", "multiline", required=True,
                  hint="What to run. Given a string it goes through the "
                       "shell, so pipes and redirection work; given a list it "
                       "is run directly, which is safer with paths that have "
                       "spaces in them."),
            field("dir", "Working directory", "dir",
                  hint="Where to run it. Relative paths are read against the "
                       "run's workspace. Blank uses the workspace itself."),
            field("env", "Environment", "env",
                  hint="Extra variables, on top of the ones this process has."),
            field("shell", "Use a shell", "check", default=True,
                  hint="Off runs the command directly with no shell involved."),
            field("expect_exit", "Expected exit code", "number", default=0,
                  hint="What counts as success. Use -1 to accept any code and "
                       "decide in a later step from ${steps.<id>.outputs."
                       "exit_code}."),
        ),
        outputs=(
            output("exit_code", "number", "What the command exited with."),
            output("stdout_tail", "string", "The last lines of its output."),
            output("stdout_path", "string", "Where all of stdout was written."),
            output("stderr_path", "string", "Where all of stderr was written."),
        ),
    )

    def problems(self, settings):
        found = super(ShellCommand, self).problems(settings)
        command = (settings or {}).get("command")
        if isinstance(command, (list, tuple)):
            if not [part for part in command if str(part).strip()]:
                found.append("Command is an empty list.")
        elif isinstance(command, str) and command.strip():
            if not settings.get("shell", True):
                try:
                    if not _split(command):
                        found.append("Command is empty once it is split.")
                except ValueError as exc:
                    found.append("Command: %s." % exc)
        return found

    def execute(self, context, step):
        command, shell = self._command(step.settings)
        workdir = self._workdir(context, step)
        environment = dict(context.environment)
        environment.update(self.setting(step.settings, "env") or {})
        expected = int(float(self.setting(step.settings, "expect_exit", 0)))

        out_path = os.path.join(context.step_dir(step.id), "stdout.log")
        err_path = os.path.join(context.step_dir(step.id), "stderr.log")

        try:
            process = subprocess.Popen(
                command, cwd=workdir, env=environment, shell=shell,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # Its own process group, so stopping the run reaches anything
                # the command itself started - a `make` that spawned a compiler,
                # a script that spawned a server.
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            return registry.failed("cannot run it: %s" % exc)

        tail = []
        with open(out_path, "wb") as out_file, open(err_path, "wb") as err_file:
            pumps = [_Pump(process.stdout, out_file, "out", context, step.id, tail),
                     _Pump(process.stderr, err_file, "err", context, step.id, None)]
            for pump in pumps:
                pump.start()
            code = self._wait(process, context)
            for pump in pumps:
                pump.join(FLUSH_SECONDS * 4)

        artifacts = [
            Artifact("log", context.relative(out_path), step.id, name="stdout",
                     bytes=_size(out_path)),
            Artifact("log", context.relative(err_path), step.id, name="stderr",
                     bytes=_size(err_path)),
        ]
        outputs = {"exit_code": code,
                   "stdout_tail": "\n".join(tail[-TAIL_LINES:]),
                   "stdout_path": context.relative(out_path),
                   "stderr_path": context.relative(err_path)}

        if context.cancel.is_set():
            # Deliberately not the cancel token's reason: the executor prefixes
            # that when it records the step, and repeating it here would print
            # it twice. This says only what this plugin knows.
            return registry.PluginResult(
                "failed", outputs=outputs, artifacts=artifacts,
                message="the command was signalled and did not finish")
        if expected >= 0 and code != expected:
            return registry.PluginResult(
                "failed", outputs=outputs, artifacts=artifacts,
                message="exited %s, expected %s%s"
                        % (code, expected, _because(tail)))
        return registry.PluginResult("success", outputs=outputs,
                                     artifacts=artifacts,
                                     message="exited %s" % code)

    # -- the pieces -----------------------------------------------------------
    def _command(self, settings):
        """``(argv or string, shell)``, honouring what was actually written.

        Three cases, and the third is the one worth spelling out:

        * a **list** always runs directly. Somebody who wrote a list did so to
          avoid the shell's quoting, and handing it to one anyway would undo
          the only thing they were asking for.
        * a **string with a shell** goes across as it is, so pipes, redirection
          and ``&&`` mean what they look like.
        * a **string without a shell** has to be split here, the way a shell
          would have split it - otherwise the whole line arrives as one
          filename. :meth:`problems` already checks that it *can* be split, so
          this is the other half of that promise.
        """
        command = (settings or {}).get("command")
        if isinstance(command, (list, tuple)):
            return [str(part) for part in command], False
        text = str(command or "")
        if self.setting(settings, "shell", True):
            return text, True
        return _split(text), False

    def _workdir(self, context, step):
        """Where to run, with a relative path read against the workspace."""
        where = str(self.setting(step.settings, "dir") or "").strip()
        if not where:
            return context.workspace
        where = os.path.expanduser(where)
        if not os.path.isabs(where):
            where = os.path.join(context.workspace, where)
        os.makedirs(where, exist_ok=True)
        return where

    def _wait(self, process, context):
        """Wait for it, stopping it if the run is cancelled or time runs out.

        Polls rather than blocking on ``wait()`` so the cancel token is noticed;
        the tick is what the token itself uses, so this costs nothing when
        nothing is happening.
        """
        while True:
            try:
                return process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            if context.cancel.is_set():
                return self._stop(process)

    def _stop(self, process):
        """Signal it, then kill it, and say which happened."""
        _signal_group(process, hard=False)
        try:
            return process.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            pass
        _signal_group(process, hard=True)
        try:
            return process.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            return -1


class _Pump(threading.Thread):
    """Read one stream to a file, batching what it says onto the event stream.

    A thread per stream because ``stdout`` and ``stderr`` are separate pipes and
    reading them one after the other deadlocks the moment either fills. Written
    to the file line by line as it arrives, so a killed process still leaves a
    complete log up to the point it died.
    """

    daemon = True

    def __init__(self, stream, handle, name, context, step_id, tail,
                 on_line=None):
        threading.Thread.__init__(self, name="cycle-%s-%s" % (step_id, name))
        self._stream = stream
        self._handle = handle
        self._name = name
        self._context = context
        self._step_id = step_id
        self._tail = tail
        # Given, this stream is structured and the handler decides what is
        # worth showing: the raw lines still reach the file and the tail, but
        # never the log. An agent's event stream is JSON - printing it verbatim
        # is how a step's output became an unreadable wall.
        self._on_line = on_line

    def run(self):
        batch, last = [], time.monotonic()
        try:
            for raw in iter(self._stream.readline, b""):
                self._handle.write(raw)
                self._handle.flush()
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if self._tail is not None:
                    self._tail.append(line)
                    del self._tail[:-TAIL_LINES]
                if self._on_line is not None:
                    self._deliver(line)
                    continue
                batch.append(line)
                now = time.monotonic()
                if now - last >= FLUSH_SECONDS or len(batch) >= 200:
                    self._context.log(self._step_id, self._name, batch)
                    batch, last = [], now
        except (OSError, ValueError):
            pass                              # the pipe closed under us
        finally:
            if batch:
                self._context.log(self._step_id, self._name, batch)
            try:
                self._stream.close()
            except OSError:
                pass


    def _deliver(self, line):
        """Hand one line to the handler. A handler that throws loses its line.

        Never the step: this is a reader of somebody else's output format, and
        a line it cannot make sense of is a row missing from a panel, not a
        failed run.
        """
        try:
            self._on_line(line)
        except Exception:                     # noqa: BLE001 - diagnostics only
            log.debug("stage handler refused a line", exc_info=True)


def _signal_group(process, hard):
    """Stop the command and anything it started, on either platform."""
    try:
        if os.name == "nt":
            process.kill() if hard else process.terminate()
            return
        import signal
        os.killpg(os.getpgid(process.pid),
                  signal.SIGKILL if hard else signal.SIGTERM)
    except (OSError, ProcessLookupError, PermissionError):
        try:
            process.kill() if hard else process.terminate()
        except OSError:
            pass


def _split(text):
    """Split a command line the way a shell would, without running one.

    The same two-platform care ``runnertypes.split_args`` takes in the GUI, and
    for the same reason - this cannot import that one, because the GUI depends
    on PySide6 and the core must not. Raises ``ValueError`` on an unbalanced
    quote, which :meth:`ShellCommand.problems` turns into a message rather than
    a crash halfway through starting something.
    """
    import shlex
    if not text or not text.strip():
        return []
    if os.name == "nt":
        # posix=False, because the posix lexer treats a backslash as an escape
        # and would eat every separator in C:\path\to\thing. It leaves the
        # quotes on a quoted token, though, and the child must never see those.
        return [token[1:-1] if len(token) > 1 and token[0] == token[-1] == '"'
                else token
                for token in shlex.split(text, posix=False)]
    return shlex.split(text)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _because(tail):
    """The last line of output, when there is one worth quoting in a message."""
    for line in reversed(tail):
        if line.strip():
            return ": %s" % line.strip()[:160]
    return ""
