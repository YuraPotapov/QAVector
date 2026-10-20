"""Client for services isolated from the command line (``execution`` in
services.json, a deployment built with the runtime commands). OS enforcement
stays in the core process; restrictions set in the GUI go through
``restrictions.exec_argv`` instead."""

import json

from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from .runtime_services_config import execution_problems


def command(core, *arguments):
    if core is None or not core.is_configured() or not core.prefix:
        raise ValueError("The QAVector core is unavailable; the service was not started.")
    return list(core.prefix) + list(arguments)


class RuntimeServiceProcess(QObject):
    ready = Signal(dict)
    line = Signal(str)
    ended = Signal(int, dict)

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._stderr)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.process.started.connect(self._started)
        self._buffer = b""
        self._text = {"stdout": "", "stderr": ""}
        self._terminal = None
        self._problem = ""
        self._finished_once = False
        self._stopping = False
        self._initial_data = b""

    def start(self, execution, packet):
        problems = execution_problems(execution)
        if problems:
            raise ValueError(problems[0])
        encoded = (json.dumps(packet, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("The isolated service request exceeds its size limit.")
        argv = command(self.core, "--runtime-service=" + execution["deployment"],
                       "--runtime-service-revision=" + execution["revision"])
        self.process.setWorkingDirectory(self.core.spawn_dir() or "")
        self._initial_data = encoded
        self.process.start(argv[0], argv[1:])

    def _started(self):
        self.process.write(self._initial_data)
        self._initial_data = b""
        if self._stopping:
            self.process.write(b'{"op":"stop"}\n')

    def is_running(self):
        return self.process.state() != QProcess.NotRunning

    def stop(self):
        if not self.is_running() or self._stopping:
            return False
        self._stopping = True
        if self.process.state() == QProcess.Running:
            self.process.write(b'{"op":"stop"}\n')
        # The core enforces the requested grace. If it becomes unresponsive,
        # parent death still tears down the native namespace; an absent end
        # record never authorizes an automatic restart.
        QTimer.singleShot(75000, self._kill_unresponsive)
        return True

    def _kill_unresponsive(self):
        if self._stopping and self.is_running():
            self._problem = "The runtime supervisor did not confirm shutdown. Review recovery before restarting."
            self.process.kill()

    def _text_chunk(self, stream, text):
        if stream not in self._text or not isinstance(text, str):
            self._protocol_error()
            return
        self._text[stream] += text
        while "\n" in self._text[stream] or len(self._text[stream]) > 8192:
            if "\n" in self._text[stream][:8193]:
                line, self._text[stream] = self._text[stream].split("\n", 1)
            else:
                line, self._text[stream] = self._text[stream][:8192], self._text[stream][8192:]
            self.line.emit(line.rstrip("\r"))

    def _read(self):
        self._buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            if len(raw) > 8 * 1024 * 1024:
                self._protocol_error(); return
            try:
                packet = json.loads(raw)
                if not isinstance(packet, dict) or type(packet.get("protocol_version")) is not int or packet["protocol_version"] != 1:
                    raise ValueError()
                kind = packet.get("kind")
                if kind == "runtime.service.output":
                    self._text_chunk(packet.get("stream"), packet.get("text"))
                elif kind == "runtime.service.ready":
                    if packet.get("enforcement") != "verified":
                        raise ValueError()
                    self.ready.emit(packet)
                elif kind == "runtime.service.error":
                    problem = packet.get("problem", {})
                    self._problem = str(problem.get("message", "Isolated service launch failed."))
                    self.line.emit(self._problem)
                elif kind == "runtime.service.end":
                    self._terminal = packet
                elif not isinstance(kind, str) or not kind.startswith("runtime."):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                self._protocol_error(); return
        if len(self._buffer) > 8 * 1024 * 1024:
            self._protocol_error()

    def _stderr(self):
        text = bytes(self.process.readAllStandardError()).decode("utf-8", "replace")
        self._text_chunk("stderr", text[:65536])

    def _protocol_error(self):
        self._problem = "The core returned an invalid runtime service response; no host fallback was attempted."
        self.process.kill()

    def _error(self, error):
        if error == QProcess.FailedToStart:
            self._problem = "Cannot start the QAVector runtime supervisor."
            self._finished(1, QProcess.CrashExit)

    def _finished(self, code, _status):
        if self._finished_once:
            return
        self._finished_once = True
        self._initial_data = b""
        self._read()
        self._stderr()
        for stream, text in self._text.items():
            if text:
                self.line.emit(text)
                self._text[stream] = ""
        terminal = dict(self._terminal or {})
        terminal["problem"] = self._problem
        if not self._terminal or self._buffer:
            terminal["cleanup_confirmed"] = False
            terminal["ok"] = False
            terminal["problem"] = self._problem or "The runtime did not confirm cleanup; automatic restart is blocked."
        self.ended.emit(code, terminal)

    def wait(self, timeout_ms):
        return not self.is_running() or self.process.waitForFinished(timeout_ms)
