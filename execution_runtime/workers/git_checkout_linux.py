"""Standalone Linux workload artifact. Run only inside an approved runtime image.

The control process supplies data and an approved Git executable. Clone,
checkout, atomic publication and attempt-owned cleanup all run in this process.
No QAVector imports or host configuration are needed inside the image.
"""

import ctypes
import errno
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

CONTRACT = "qavector.git-checkout/v1"
MAX_PACKET = 16384


class WorkerError(Exception):
    pass


def packet(raw):
    if len(raw.encode("utf-8")) > MAX_PACKET:
        raise WorkerError("worker_input_too_large")
    value = json.loads(raw)
    fields = {"contract", "operation", "nonce", "base", "repository", "path", "branch", "commit", "depth", "git"}
    if (not isinstance(value, dict) or set(value) != fields or value["contract"] != CONTRACT or
            value["operation"] not in ("checkout", "cleanup") or
            not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"]) or
            any(not isinstance(value[key], str) or "\0" in value[key] or len(value[key]) > 4096
                for key in ("base", "repository", "path", "branch", "commit", "git")) or
            type(value["depth"]) is not int or not 0 <= value["depth"] <= 2147483647):
        raise WorkerError("invalid_worker_input")
    if (not Path(value["base"]).is_absolute() or not Path(value["git"]).is_absolute() or
            not Path(value["repository"]).is_absolute() or
            not value["path"] or any(part in ("", ".", "..", ".git") for part in value["path"].split("/")) or
            any(c in value["path"] for c in ("\\", ":")) or value["path"].startswith("/")):
        raise WorkerError("invalid_worker_path")
    return value


def locations(value):
    base = Path(value["base"]).resolve(strict=True)
    target = base.joinpath(*value["path"].split("/"))
    parent = target.parent.resolve()
    if parent != base and base not in parent.parents:
        raise WorkerError("checkout_path_escape")
    target = parent / target.name
    if target.name.startswith(".qavector-checkout-"):
        raise WorkerError("reserved_checkout_path")
    stage = parent / (".qavector-checkout-" + value["nonce"])
    owner = {"contract": CONTRACT, "nonce": value["nonce"], "destination": str(target)}
    return base, target, stage, owner


def cleanup(value):
    _, _, stage, owner = locations(value)
    if not os.path.lexists(stage):
        return
    if stage.is_symlink() or not stage.is_dir():
        raise WorkerError("unsafe_checkout_staging")
    marker = stage / "owner.json"
    if marker.is_symlink() or marker.stat().st_size > 8192 or json.loads(marker.read_text()) != owner:
        raise WorkerError("checkout_owner_mismatch")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise WorkerError("safe_cleanup_unavailable")
    shutil.rmtree(stage)


def publish(source, target):
    """Linux's exclusive atomic rename; never overwrite even an empty directory."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise WorkerError("atomic_publish_unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise WorkerError("checkout_destination_exists" if code in (errno.EEXIST, errno.ENOTEMPTY)
                          else "checkout_publish_failed")


def git(value, hooks, args, cwd, capture=False):
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_COUNT": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file"}
    options = [value["git"], "-c", "core.hooksPath=" + str(hooks), "-c", "init.templateDir=" + str(hooks),
               "-c", "core.fsmonitor=false", "-c", "credential.helper=", "-c", "protocol.allow=never",
               "-c", "protocol.file.allow=always", "-c", "maintenance.auto=false", "-c", "gc.auto=0"]
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(options + args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=output if capture else sys.stderr, stderr=sys.stderr)
        try:
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if code:
            raise WorkerError("git_command_failed")
        if capture:
            output.seek(0)
            raw = output.read(8193)
            if len(raw) > 8192:
                raise WorkerError("git_result_too_large")
            return raw.decode("utf-8", "strict").strip()
    return ""


def checkout(value):
    _, target, stage, owner = locations(value)
    if os.path.lexists(target):
        raise WorkerError("checkout_destination_exists")
    repository = Path(value["repository"]).resolve(strict=True)
    if not repository.is_dir():
        raise WorkerError("local_repository_required")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir(mode=0o700)
    marker = stage / "owner.json"
    with marker.open("x") as handle:
        json.dump(owner, handle)
    hooks = stage / "no-hooks"
    hooks.mkdir(mode=0o500)
    clone = stage / "repository"
    args = ["clone", "--no-hardlinks", "--dissociate", "--no-checkout"]
    if value["branch"]:
        args.append("--branch=" + value["branch"])
    if value["depth"]:
        # A local-copy clone ignores depth; fail rather than claim a shallow clone.
        raise WorkerError("local_clone_depth_unsupported")
    git(value, hooks, args + ["--", str(repository), str(clone)], target.parent)
    revision = git(value, hooks, ["rev-parse", "--verify", "--end-of-options", (value["commit"] or "HEAD") + "^{commit}"], clone, True)
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
        raise WorkerError("invalid_git_revision")
    git(value, hooks, ["checkout", "--detach", revision] if value["commit"] else ["checkout", "--force"], clone)
    branch = git(value, hooks, ["rev-parse", "--abbrev-ref", "HEAD"], clone, True)
    if len(branch) > 1024 or any(c in branch for c in ("\n", "\r", "\0")):
        raise WorkerError("invalid_git_branch")
    # Do not persist an ephemeral hook/template path into the resulting checkout.
    # The restrictions above are command-scoped - clone has no template hooks.
    publish(clone, target)
    return {"commit": revision, "branch": branch}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    value, outputs, code, status = None, {}, "", "failed"
    try:
        if sys.platform != "linux" or len(args) != 1:
            raise WorkerError("unsupported_worker_invocation")
        value = packet(args[0])
        if value["operation"] == "cleanup":
            cleanup(value)
        else:
            outputs = checkout(value)
        status = "success"
    except WorkerError as exc:
        code = str(exc)
    except (OSError, ValueError, UnicodeError):
        code = "git_worker_io_error"
    except KeyboardInterrupt:
        code = "git_worker_interrupted"
    finally:
        # Native cancellation can terminate this process before finally runs.
        # The supervisor therefore repeats only this cleanup in a new instance.
        if value is not None:
            try:
                cleanup(value)
            except (OSError, ValueError, WorkerError):
                status, code = "failed", "checkout_cleanup_failed"
    print(json.dumps({"contract": CONTRACT, "status": status, "outputs": outputs if status == "success" else {}, "code": code}))
    return 0 if status == "success" else 1


if __name__ == "__main__":
    def interrupted(_signal, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
