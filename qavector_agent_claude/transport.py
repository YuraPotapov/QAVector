"""Local Claude subprocesses inside the already isolated SDK worker process."""

import asyncio
import json
import os

from qavector_sdk import ContractError
from qavector_sdk.worker import host_call, require_worker_context

OUTPUT_BYTES = 2 * 1024 * 1024


class WorkerTransport:
    streaming = True

    def __init__(self):
        self.context = None

    async def prerequisites(self, profile, context):
        require_worker_context(context)
        binary = profile["backend_options"]["binary"]
        if not os.path.isabs(binary) or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
            raise ContractError("AGENT_PREREQUISITE_MISSING", "Select an absolute executable in the approved worker image")
        try:
            process = await asyncio.create_subprocess_exec(binary, "auth", "status", "--json",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=65536)
            try:
                async def read_status():
                    raw = bytearray()
                    while True:
                        chunk = await process.stdout.read(65537 - len(raw))
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise ValueError("status too large")
                    await process.wait()
                    value = json.loads(raw)
                    return process.returncode == 0 and isinstance(value, dict) and value.get("loggedIn") is True
                ready = await asyncio.wait_for(read_status(), timeout=20)
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
        except (OSError, ValueError, asyncio.TimeoutError):
            raise ContractError("AGENT_PREREQUISITE_MISSING", "Claude authentication is unavailable in the selected worker scope") from None
        if not ready:
            raise ContractError("AGENT_PREREQUISITE_MISSING", "Claude is not signed in within the selected worker scope")
        self.context = dict(context)

    async def workspace(self, reference, context):
        require_worker_context(context)
        if reference is None:
            return os.getcwd()
        resolved = await host_call(context["runtime_handle"], "workspace.resolve", {"workspace_ref": reference})
        if not isinstance(resolved, dict) or set(resolved) != {"path"} or not isinstance(resolved["path"], str):
            raise ContractError("POLICY_UNSATISFIED", "Runtime did not resolve the workspace reference")
        path = resolved["path"]
        if not os.path.isabs(path) or not os.path.isdir(path):
            raise ContractError("POLICY_UNSATISFIED", "Workspace is unavailable inside this worker")
        return path

    async def run(self, argv, cwd, on_line):
        # The parent worker is already inside Runtime's OS boundary. Child
        # processes inherit it; only Runtime can attest full descendant exit.
        if self.context is None:
            raise ContractError("AGENT_PREREQUISITE_MISSING", "Check worker prerequisites before execution")
        require_worker_context(self.context)
        environment = dict(os.environ)
        environment.pop("CLAUDE_CODE_SIMPLE", None)
        try:
            process = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=environment,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=OUTPUT_BYTES)
        except OSError:
            return {"ok": False, "started": False, "message": "Cannot start Claude in the selected worker scope"}
        try:
            total = 0
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Claude stream line exceeds its limit") from None
                if not line:
                    break
                total += len(line)
                if total > OUTPUT_BYTES:
                    raise ContractError("WORKER_PROTOCOL_ERROR", "Claude output exceeds its limit")
                await on_line(line.decode("utf-8", "replace"))
            code = await process.wait()
            return {"ok": code == 0, "started": True, "message": "" if code == 0 else "Claude process exited unsuccessfully"}
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
