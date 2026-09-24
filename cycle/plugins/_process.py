"""Subprocess transport shared by Git and framework workers.

Reuse the command plugin's log pump and cancellation policy. Each invocation
gets separate log files, so a multi-command plugin keeps all its diagnostics.
"""

import os
import subprocess
import tempfile

import runtime_paths
from cycle import registry
from cycle.plugins.command import (FLUSH_SECONDS, STOP_GRACE, ShellCommand,
                                   _Pump, _signal_group, _size)
from domain.cycle import Artifact


def run_process(context, step, argv, cwd, name, env=None, on_stdout=None):
    """Run argv without a shell or interactive stdin; return logs and status.

    ``on_stdout``, when given, receives each stdout line instead of the log
    doing: the caller is reading a structured stream and decides what of it is
    worth showing. stdout still reaches the file and the tail either way, so
    nothing is lost - only the verbatim copy in the panel.
    """
    if context.cancel.is_set():
        return registry.failed("cancelled before starting %s" % name, started=False)
    directory = tempfile.mkdtemp(prefix=name + "-", dir=context.step_dir(step.id))
    out_path = os.path.join(directory, "stdout.log")
    err_path = os.path.join(directory, "stderr.log")
    environment = runtime_paths.clean_subprocess_env(context.environment)
    for key, value in (env or {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = str(value)
    tail, errors = [], []
    with open(out_path, "wb") as out_file, open(err_path, "wb") as err_file:
        try:
            process = subprocess.Popen(
                argv, cwd=cwd, env=environment, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=(os.name != "nt"))
        except OSError as exc:
            return registry.failed("cannot start %s: %s" % (name, exc), started=False)
        pumps = [
            _Pump(process.stdout, out_file, "out", context, step.id, tail,
                  on_line=on_stdout),
            _Pump(process.stderr, err_file, "err", context, step.id, errors),
        ]
        try:
            for pump in pumps:
                pump.start()
            code = ShellCommand()._wait(process, context)
        finally:
            if process.poll() is None:
                ShellCommand()._stop(process)
            for pump in pumps:
                if pump.ident is not None:
                    pump.join(STOP_GRACE)
            if any(pump.is_alive() for pump in pumps):
                # A child can inherit the pipes even after its parent exits.
                _signal_group(process, hard=True)
                for pump in pumps:
                    if pump.ident is not None:
                        pump.join(FLUSH_SECONDS * 4)

    # Both tails, because a command that failed usually says why on stderr and
    # a caller handed only stdout would be reporting silence.
    outputs = {"exit_code": code, "stdout_tail": "\n".join(tail),
               "stderr_tail": "\n".join(errors),
               "stdout_path": context.relative(out_path),
               "stderr_path": context.relative(err_path)}
    artifacts = [Artifact("log", context.relative(path), step.id,
                          name=name + "." + stream, bytes=_size(path))
                 for path, stream in ((out_path, "stdout"), (err_path, "stderr"))]
    ok = code == 0 and not context.cancel.is_set()
    message = "%s exited %s" % (name, code)
    if context.cancel.is_set():
        message = "%s was stopped" % name
    elif not ok and (errors or tail):
        message += ": " + (errors or tail)[-1][:300]
    return registry.PluginResult("success" if ok else "failed",
                                 outputs=outputs, artifacts=artifacts,
                                 message=message)
