"""One turn of a conversation with an agent, read while it happens.

Every other question the GUI asks the core is a process started and waited for:
one answer, at the end. A turn is the exception. The core prints a line per
event as the agent works - what it read, what it said - and the answer only as
its last line, so this reads the process as it runs and hands each event on the
moment it arrives. No thread is involved: a ``QProcess`` tells the event loop
when there is something to read, and the window keeps painting between lines.

What the core prints is already on disk by the time it is printed (see
``integrations/agents/runner.py``), so nothing here is the record. A window
closed mid-turn loses nothing; it only stops being told.

**Stop** ends the core's process the polite way, which the core takes as "stop
the agent": it ends the agent's program, writes the turn down as stopped, and
says so in its last line - so a stopped turn is answered like any other.
"""

import codecs
import json
import os
import tempfile

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

#: How long a stopped core is given to end the agent and answer before it is
#: killed. Longer than the core's own grace for the agent's program.
STOP_GRACE_MS = 9000

#: How much of what the core wrote to stderr is quoted when it ends unanswered.
ERROR_CHARS = 400


class AgentTurn(QObject):
    """``agents send SESSION``, started and read line by line."""

    #: One event of the turn, as the core recorded it.
    event = Signal(dict)
    #: The turn is over: the core's answer, or why there is none. Emitted once.
    answered = Signal(dict)

    def __init__(self, core, session_id, document, parent=None):
        super().__init__(parent)
        self.core = core
        self.session_id = session_id
        self.document = dict(document or {})
        self._proc = None
        self._source = ""
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._error_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._errors = ""
        self._answer = None
        self._done = False
        self._stopping = False

    # -- lifecycle --------------------------------------------------------------
    def is_running(self):
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def start(self):
        """Start the turn. However it goes, ``answered`` says how - once."""
        if self._proc is not None:
            return
        try:
            # What is said goes in a file only this account can read, never on
            # the command line, which every process on the machine can.
            handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json",
                                                 prefix="cms-agent-turn-", delete=False)
            with handle:
                json.dump(self.document, handle, ensure_ascii=False)
            os.chmod(handle.name, 0o600)
            self._source = handle.name
            argv = self.core.agents_argv("send", self.session_id, source=self._source)
        except (OSError, AttributeError) as exc:
            self._finish({"ok": False, "kind": "error",
                          "error": "The turn could not be started: %s" % exc})
            return
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._read)
        proc.readyReadStandardError.connect(self._read_errors)
        proc.finished.connect(self._ended)
        proc.errorOccurred.connect(self._error)
        where = self.core.spawn_dir()
        if where:
            proc.setWorkingDirectory(where)
        self._proc = proc
        proc.start(argv[0], list(argv[1:]))

    def stop(self):
        """Ask the core to stop the agent. The turn still answers, as stopped."""
        if not self.is_running() or self._stopping:
            return
        self._stopping = True
        self._proc.terminate()
        # Parented to the process, so a turn that ends in time cancels it.
        timer = QTimer(self._proc)
        timer.setSingleShot(True)
        timer.timeout.connect(self._kill)
        timer.start(STOP_GRACE_MS)

    def shutdown(self, wait_ms=STOP_GRACE_MS):
        """End the process before this object goes - a ``QProcess`` destroyed
        while its program runs takes the application with it."""
        if not self.is_running():
            return
        self.stop()
        if not self._proc.waitForFinished(wait_ms):
            self._proc.kill()
            self._proc.waitForFinished(2000)

    def _kill(self):
        if self.is_running():
            self._proc.kill()

    # -- reading ----------------------------------------------------------------
    def _read(self):
        self._buffer += self._decoder.decode(bytes(self._proc.readAllStandardOutput()))
        if "\n" not in self._buffer:
            return
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()          # a line still arriving waits for its end
        for line in lines:
            self._line(line)

    def _line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except ValueError:
            return                          # not the core's: nothing to show for it
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("event"), dict):
            self.event.emit(payload["event"])
        elif "ok" in payload:
            self._answer = payload

    def _read_errors(self):
        self._errors += self._error_decoder.decode(bytes(self._proc.readAllStandardError()))
        self._errors = self._errors[-ERROR_CHARS * 4:]

    def _ended(self, code, _status):
        self._read()
        self._read_errors()
        self._buffer += self._decoder.decode(b"", final=True)
        self._errors += self._error_decoder.decode(b"", final=True)
        if self._buffer.strip():
            self._line(self._buffer)
            self._buffer = ""
        if self._answer is not None:
            self._finish(self._answer)
            return
        said = self._errors.strip()[-ERROR_CHARS:]
        self._finish({"ok": False, "kind": "stopped" if self._stopping else "error",
                      "record_unknown": True,
                      "error": ("The turn was stopped." if self._stopping else
                                "The core ended without an answer (exit %s)%s"
                                % (code, ": %s" % said if said else "."))})

    def _error(self, error):
        if error == QProcess.FailedToStart:
            self._finish({"ok": False, "kind": "error",
                          "error": "Could not start %s - check the interpreter and core "
                                   "script in Settings." % (self._proc.program(),)})

    def _finish(self, answer):
        if self._done:
            return
        self._done = True
        if self._source:
            try:
                os.unlink(self._source)
            except OSError:
                pass
            self._source = ""
        self.answered.emit(dict(answer))
