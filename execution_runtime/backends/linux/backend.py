"""Native Linux isolation. Only explicitly approved offline images can run."""

import json
import math
import os
import platform as host_platform
from pathlib import Path, PurePosixPath
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace

from ..._json import Snapshot, object_fields
from ...errors import RuntimePolicyError
from ...policy import PolicyLayer, PolicyPatch, PolicyResolver
from ...registry import _atomic, default_directory
from ...secrets import SecretValues
from ..base import (Capability, CapabilitySet, ExecutionHandle, ExecutionRequest,
                    PreparedExecution, PreflightReport, RuntimeBackend)
from .cgroups import CgroupParent
from .filesystem import RuntimeFile, RuntimeImage, open_pinned, overlaps, reject_nested_mounts, target_path
from .seccomp import filter_file
from .supervisor import Instance, LogCapture
from .launcher import ApprovedLauncher
from .bundled import BundledLauncher
from .scratch import scratch_size, cgroup_limits
from .secrets import SECRET_DIRECTORY, sealed_secret, secret_path
from .recovery import (INSTANCE_ID, lock_file, new_instance,
                       process_identity, recover_instance, state_lock)


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


class LinuxBackend(RuntimeBackend):
    provider = "linux-isolated"
    platform = "linux"
    launcher_source = "bundled"

    def __init__(self, image=None, state_directory=None, cgroup_parent=None, protected_paths=(), launcher=None, bundle_directory=None):
        if image is not None and not isinstance(image, RuntimeImage):
            raise RuntimePolicyError("invalid_image", "Expected an approved RuntimeImage.")
        if image is not None and state_directory is None:
            raise RuntimePolicyError("runtime_not_configured", "An image needs a private control-plane state directory.")
        self.image = image
        self.state_directory = Path(state_directory).absolute() if state_directory is not None else None
        self.cgroup_parent = cgroup_parent
        self.protected_paths = tuple(str(Path(one).absolute()) for one in protected_paths)
        if launcher is not None and not isinstance(launcher, ApprovedLauncher):
            raise RuntimePolicyError("invalid_launcher", "Expected an explicitly approved launcher manifest.")
        self.launcher = launcher
        self._bundle = BundledLauncher(bundle_directory)
        self._launcher_metadata = None
        self._jobs = {}
        self._lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._probe_result = None
        self._scratch_result = None
        self._scratch_probe_lock = threading.Lock()
        self._secret_result = None
        self._secret_probe_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._closed = False

    def _state(self):
        path = self.state_directory
        parent = open_pinned(str(path.parent), directory=True)
        try:
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            fd = open_pinned(str(path), directory=True)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise RuntimePolicyError("unsafe_state_directory", "Runtime state must be owned by this user with private permissions (0700).")
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def _binary(self):
        if self.launcher is not None:
            self._state()
            return self.launcher.materialize(self.state_directory)
        if self.launcher_source == "bundled":
            self._launcher_metadata = self._bundle.executable()
            return self._launcher_metadata["path"]
        candidate = shutil.which("bwrap", path="/usr/bin:/bin")
        if candidate is None:
            raise RuntimePolicyError("bwrap_unavailable", "Bubblewrap is required for Linux execution.")
        binary = os.path.realpath(candidate)
        info = os.stat(binary)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimePolicyError("unsafe_launcher", "Bubblewrap must be root-owned and not writable by others.")
        return binary

    def bundle_info(self):
        return self._bundle.executable()

    def configure(self, options, protected_paths=()):
        object_fields(options, ("linux",), "backend_options", ("linux",))
        native = options["linux"]
        object_fields(native, ("image", "state_directory", "cgroup_parent", "protected_paths"),
                      "backend_options.linux", ("image", "state_directory"))
        image = native["image"]
        object_fields(image, ("identity", "files"), "image", ("identity", "files"))
        if not isinstance(image["files"], list):
            raise RuntimePolicyError("invalid_image", "Runtime files must be a pinned list.")
        files = []
        for one in image["files"]:
            object_fields(one, ("source", "target", "sha256", "executable"), "image.files",
                          ("source", "target", "sha256", "executable"))
            files.append(RuntimeFile(**one))
        approved = RuntimeImage(tuple(files))
        if approved.identity != image["identity"]:
            raise RuntimePolicyError("runtime_image_mismatch", "Deployment image identity differs from its approved files.")
        for name in ("state_directory", "cgroup_parent"):
            value = native.get(name)
            if value is not None and (not isinstance(value, str) or not value.startswith("/") or "\0" in value):
                raise RuntimePolicyError("invalid_deployment", "Native state and delegation paths must be absolute.")
        protected = native.get("protected_paths", [])
        if not isinstance(protected, list) or any(not isinstance(one, str) or not one.startswith("/") for one in protected):
            raise RuntimePolicyError("invalid_deployment", "Protected paths must be explicit absolute paths.")
        return type(self)(approved, native["state_directory"], cgroup_parent=native.get("cgroup_parent"),
                          protected_paths=tuple(protected_paths) + tuple(protected))

    def deployment_draft(self, request):
        from .provisioning import draft
        return draft(request)

    def runtime_root_identity(self):
        if self.image is None:
            raise RuntimePolicyError("runtime_not_configured", "No approved runtime image is configured.")
        return self.image.identity

    def runtime_file_identity(self, target):
        if self.image is not None:
            entry = next((one for one in self.image.files if one.target == target), None)
            if entry is not None:
                return entry.sha256
        raise RuntimePolicyError("worker_file_not_pinned", "Worker dependencies must be part of the approved runtime image.")

    def secret_file_locations(self, names):
        approved = PolicyPatch({"secrets": list(names)}).to_document()["secrets"]
        return {name: secret_path(name) for name in approved}

    def service_scope(self, key):
        from .service_scope import ServiceScope
        return ServiceScope(self, key)

    def record_event(self, record):
        if self.state_directory is None:
            raise RuntimePolicyError("runtime_audit_unavailable", "Execution audit requires a private state directory.")
        try:
            self._state()
            directory = self.state_directory / "audit"
            directory.mkdir(mode=0o700, exist_ok=True)
            fd = open_pinned(str(directory), directory=True)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise RuntimePolicyError("runtime_audit_unavailable", "Execution audit storage must be private.")
            finally:
                os.close(fd)
            path = directory / (uuid.uuid4().hex + ".json")
            _atomic(path, record)
            return str(path)
        except (OSError, RuntimePolicyError):
            raise RuntimePolicyError("runtime_audit_unavailable", "Cannot persist a private execution audit record.") from None

    def capabilities(self):
        with self._lifecycle_lock:
            return self._capabilities_open()

    def _capabilities_open(self):
        controllers = _read("/sys/fs/cgroup/cgroup.controllers")
        if self.launcher is not None:
            launcher_info = {"source": "approved", "identity": self.launcher.identity}
        elif self.launcher_source == "bundled":
            try:
                launcher_info = self._bundle.inspect()
            except RuntimePolicyError as exc:
                launcher_info = {"source": "bundled", "integrity": "unavailable", "reason": exc.code}
        else:
            launcher_info = {"source": "system", "explicit": True,
                             "installed": shutil.which("bwrap", path="/usr/bin:/bin") is not None}
        observations = {"bwrap": launcher_info,
                        "cgroups_v2": {"visible": controllers is not None,
                                       "root_controllers": (controllers or "").split(),
                                       "delegation_configured": self.cgroup_parent is not None},
                        "host_security": {"apparmor_userns_restriction": _read("/proc/sys/kernel/apparmor_restrict_unprivileged_userns"),
                                          "unprivileged_userns_clone": _read("/proc/sys/kernel/unprivileged_userns_clone")},
                        "native_execution_implemented": True, "runtime_image_configured": self.image is not None}
        passed, reason = False, "No approved runtime image is configured."
        if self.image is not None:
            passed, reason = self._probe()
            observations["runtime_root_identity"] = self.image.identity
        if self.launcher is not None:
            observations["launcher_identity"] = self.launcher.identity
        state = "verified" if passed else "unsupported"
        features = [Capability(name, state, reason) for name in (
            "runtime_execution", "filesystem_isolation", "environment_allowlist", "network_none",
            "descendant_containment", "privilege_restrictions", "crash_recovery", "supervisor_shutdown")]
        for capability, limits in (("limit_cpu_cores", {"cpu_cores": 1}),
                                   ("memory_charged", {"memory": {"bytes": 33554432, "accounting": "charged"}}),
                                   ("limit_tasks", {"tasks": 8})):
            verified, why = False, "An explicitly delegated cgroup v2 parent is required."
            if passed and self.cgroup_parent is not None:
                try:
                    group = CgroupParent(self.cgroup_parent).create(limits)
                    group.cleanup()
                    verified, why = True, "Kernel accepted and read back the resource controls."
                except (OSError, RuntimePolicyError) as exc:
                    why = exc.code if isinstance(exc, RuntimePolicyError) else "cgroup_delegation_unavailable"
            features.append(Capability(capability, "verified" if verified else "unsupported", why))
        verified, why = self._scratch_probe() if passed else (False, reason)
        features.append(Capability("limit_scratch_bytes", "verified" if verified else "unsupported", why))
        verified, why = self._secret_probe() if passed else (False, reason)
        features.append(Capability("secrets_grants", "verified" if verified else "unsupported", why))
        return CapabilitySet(self.provider, self.platform, tuple(features), Snapshot(observations))

    def _secret_probe(self):
        with self._secret_probe_lock:
            if self._secret_result is not None:
                return self._secret_result
            job = None
            try:
                value, name = "qavector-secret-delivery-probe", "runtime_probe"
                policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({"secrets": [name]}))],
                    platform=self.platform, provider=self.provider, runtime_root_identity=self.image.identity)
                path = shlex.quote(secret_path(name))
                script = ('test "$(/bin/busybox cat %s)" = "%s" && ! (printf changed > %s) '
                          '&& ! /bin/busybox rm %s && printf secret-probe-ok' % (path, value, path, path))
                request = ExecutionRequest(policy, ("/bin/busybox", "sh", "-c", script), "/",
                    timeout_seconds=3, secret_values={name: value})
                job = self._allocate(request)
                result = self.wait(self._launch(job))
                if result["exit_code"] != 0 or result["stdout_tail"] != "secret-probe-ok":
                    raise RuntimePolicyError("secret_delivery_probe_failed", "Read-only secret delivery probe failed.")
                self._secret_result = (True, "Native read-only anonymous secret delivery probe passed.")
            except (OSError, RuntimePolicyError) as exc:
                self._secret_result = (False, exc.code if isinstance(exc, RuntimePolicyError) else "secret_delivery_unavailable")
            finally:
                if job is not None:
                    self.cleanup(job.token)
            return self._secret_result

    def _scratch_probe(self):
        with self._scratch_probe_lock:
            if self._scratch_result is not None:
                return self._scratch_result
            job = None
            try:
                help_output = subprocess.run([self._binary(), "--help"], env={"LANG": "C"},
                    capture_output=True, timeout=3, check=True).stdout
                if b"--size " not in help_output:
                    raise RuntimePolicyError("scratch_size_unavailable", "Launcher cannot set a bounded tmpfs capacity.")
                policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({
                    "resources": {"scratch_bytes": 65536}}))], platform=self.platform,
                    provider=self.provider, runtime_root_identity=self.image.identity)
                script = "/bin/busybox stat -f -c '%T %S %b' /tmp; /bin/busybox dd if=/dev/zero of=/tmp/probe bs=4096 count=17 2>/dev/null; test $? -ne 0"
                job = self._allocate(ExecutionRequest(policy, ("/bin/busybox", "sh", "-c", script), "/", timeout_seconds=3))
                result = self.wait(self._launch(job))
                fields = result["stdout_tail"].split()
                if result["exit_code"] != 0 or len(fields) != 3 or fields[0] != "tmpfs" or int(fields[1]) * int(fields[2]) != 65536:
                    raise RuntimePolicyError("scratch_probe_failed", "Kernel tmpfs capacity/overflow probe failed.")
                self._scratch_result = (True, "Kernel tmpfs capacity and aggregate overflow probe passed.")
            except (OSError, RuntimePolicyError, ValueError, subprocess.SubprocessError) as exc:
                self._scratch_result = (False, exc.code if isinstance(exc, RuntimePolicyError) else "scratch_probe_failed")
            finally:
                if job is not None:
                    self.cleanup(job.token)
            return self._scratch_result

    def _probe(self):
        with self._probe_lock:
            if self._probe_result is not None:
                return self._probe_result
            job = None
            try:
                if host_platform.machine() != "x86_64":
                    raise RuntimePolicyError("native_architecture_unsupported", "This seccomp launcher currently supports Linux x86_64 only.")
                self._binary()
                if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                    raise RuntimePolicyError("pidfd_unavailable", "Safe native supervision requires Linux pidfds.")
                policy = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch({}))],
                    platform=self.platform, provider=self.provider, runtime_root_identity=self.image.identity)
                script = "/bin/busybox cat /proc/self/status; /bin/busybox readlink /proc/self/ns/net"
                request = ExecutionRequest(policy, ("/bin/busybox", "sh", "-c", script), "/", timeout_seconds=3)
                job = self._allocate(request)
                result = self.wait(self._launch(job))
                output = result["stdout_tail"]
                if (result["exit_code"] != 0 or "NoNewPrivs:\t1" not in output
                        or "Seccomp:\t2" not in output or "CapEff:\t0000000000000000" not in output
                        or os.readlink("/proc/self/ns/net") in output):
                    raise RuntimePolicyError("isolation_probe_failed", "Native boundary probe failed.")
                self._probe_result = (True, "Native namespace/seccomp/privilege probe passed.")
            except (OSError, RuntimePolicyError) as exc:
                self._probe_result = (False, exc.code if isinstance(exc, RuntimePolicyError) else "native_probe_unavailable")
            finally:
                if job is not None:
                    self.cleanup(job.token)
            return self._probe_result

    def preflight(self, policy):
        report = super().preflight(policy)
        matched = self.image is not None and policy.runtime_root_identity == self.image.identity
        check = Capability("approved_runtime_image", "verified" if matched else "unsupported",
                           "The policy must pin the configured image content identity.")
        checks = report.checks + (check,)
        resources = policy.policy.to_document()["resources"]
        if resources:
            try:
                scratch_size(resources)
                CgroupParent.settings(cgroup_limits(resources))
                check = Capability("requested_resource_semantics", "verified", "Requested limits have exact native mappings.")
            except RuntimePolicyError as exc:
                check = Capability("requested_resource_semantics", "unsupported", exc.code)
            checks += (check,)
        return PreflightReport(report.capabilities, policy.digest, checks)

    def gateway_preflight(self, policy, route):
        from ...policy import PolicyLayer, PolicyPatch, PolicyResolver
        route.authorize(policy)
        native = PolicyResolver().resolve([PolicyLayer("platform", PolicyPatch(policy.policy.to_document())),
            PolicyLayer("node", PolicyPatch({"network": {"mode": "none"}, "secrets": []}))],
            [one.to_document() for one in policy.bindings], platform=self.platform, provider=self.provider,
            profile_revision=policy.profile_revision, runtime_root_identity=policy.runtime_root_identity)
        report = self.preflight(native)
        state = "verified" if report.ready else "unsupported"
        feature = Capability("network_restricted", state, "Only the approved core provider broker; native TCP is confined to private loopback. Unix/raw/datagram sockets are denied.")
        capabilities = CapabilitySet(self.provider, self.platform, report.capabilities.features + (feature,), report.capabilities.observations)
        return PreflightReport(capabilities, policy.digest, report.checks + (feature,))

    def _mounts(self, policy):
        sources = []
        protected = self.protected_paths + ("/proc", "/sys", "/dev", str(self._bundle.directory), str(self.state_directory), str(Path(__file__).resolve().parents[3]),
                                             os.path.realpath(sys.executable), self._binary(), str(Path(default_directory()).absolute()))
        protected = tuple(os.path.realpath(one) for one in protected)
        image_targets = [one.target for one in self.image.files]
        try:
            for mount in policy.to_document()["resolved_filesystem"]:
                source, target = mount["source"], target_path(mount["target"])
                if (any(overlaps(source, one) for one in protected)
                        or any(overlaps(target, one) for one in ["/proc", "/dev", "/tmp", SECRET_DIRECTORY] + image_targets)):
                    raise RuntimePolicyError("protected_path", "A mount overlaps runtime/control-plane resources.")
                fd = open_pinned(source)
                sources.append((fd, target, mount["access"]))
                reject_nested_mounts(source)
            return sources
        except BaseException as exc:
            for fd, _, _ in sources:
                os.close(fd)
            if isinstance(exc, OSError):
                raise RuntimePolicyError("unsafe_source", "Workspace source cannot be pinned without following symlinks.") from None
            raise

    def _allocate(self, request):
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimePolicyError("runtime_closed", "This runtime has shut down.")
            return self._allocate_open(request)

    def _allocate_open(self, request):
        if self.image is None:
            raise RuntimePolicyError("runtime_not_configured", "No approved runtime image is configured.")
        if request.policy.provider != self.provider or request.policy.platform != self.platform:
            raise RuntimePolicyError("provider_platform_mismatch", "Request and backend must agree.")
        if request.policy.runtime_root_identity != self.image.identity:
            raise RuntimePolicyError("runtime_image_mismatch", "Policy must pin the configured runtime image.")
        raw = request.policy.policy.to_document()
        if ((raw["network"]["mode"] != "none" and request.gateway is None) or raw["tools"] or raw["integrations"]):
            raise RuntimePolicyError("unsupported_guarantee", "Only offline native execution without broker grants is supported yet.")
        if request.gateway is None and set(raw["secrets"]) != set(request.secret_values.names):
            raise RuntimePolicyError("secret_inputs_missing", "Every requested secret grant needs an explicit scoped value.")
        scratch_size(raw["resources"])
        limits = cgroup_limits(raw["resources"])
        if limits:
            CgroupParent.settings(limits)
            if self.cgroup_parent is None:
                raise RuntimePolicyError("cgroup_delegation_unavailable", "Requested quotas require explicit cgroup delegation.")
        cwd = request.working_directory
        if cwd != "/":
            target_path(cwd)
        target_path(request.argv[0])
        self._state()
        identity = uuid.uuid4().hex
        token = PreparedExecution(identity, self.provider, request.policy.digest)
        directory, lease = new_instance(self.state_directory, identity, request.policy.digest,
            self.image.identity, str(Path(self.cgroup_parent)) if limits else None, provider=self.provider)
        sources, secret_mounts, seccomp, group, root_fd, environment_fd = [], [], None, None, None, None
        try:
            sources = self._mounts(request.policy)
            root = directory / "root"
            self.image.materialize(root)
            environment = request.environment.to_document()
            if environment:
                arguments = [part for key, value in sorted(environment.items()) for part in ("--setenv", key, value)]
                environment_fd, _ = sealed_secret("runtime_environment_arguments", "\0".join(arguments) + "\0")
            for name, value in request.secret_values.items():
                fd, target = sealed_secret(name, value)
                secret_mounts.append((fd, target))
                destination = root / target.lstrip("/")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.touch(mode=0o400, exist_ok=False)
            for fd, target, _ in sources:
                destination = root / target.lstrip("/")
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.touch(exist_ok=False)
            if cwd != "/" and not any(PurePosixPath(target) == PurePosixPath(cwd) or
                   PurePosixPath(target) in PurePosixPath(cwd).parents for _, target, _ in sources):
                if not (root / cwd.lstrip("/")).is_dir():
                    raise RuntimePolicyError("working_directory_denied", "Working directory is outside the runtime grants/image.")
            seccomp = filter_file(loopback_tcp=request.gateway is not None)
            if limits:
                group = CgroupParent(self.cgroup_parent).create(limits, instance_id=identity)
            root_fd = open_pinned(str(root), directory=True)
            safe_request = replace(request, secret_values=SecretValues(), environment=Snapshot({}), gateway=None)
            job = Instance(token, safe_request, directory,
                [root_fd] + [fd for fd, _, _ in sources] + [fd for fd, _ in secret_mounts]
                + ([environment_fd] if environment_fd is not None else []), seccomp, group)
            job.environment_fd = environment_fd
            job.gateway = request.gateway
            if job.gateway is not None:
                job.gateway.bind()
            job.environment_names = sorted(environment)
            job.secret_mounts = secret_mounts
            job.secret_locations = [{"name": name, "path": secret_path(name)} for name in request.secret_values.names]
            job.secret_redactions = tuple(value for _, value in request.secret_values.items()) + tuple(environment.values())
            if job.gateway is not None:
                job.secret_redactions += job.gateway.values_for_redaction()
            job.lease_fd = lease
            job.mounts = sources
            with self._lock:
                self._jobs[identity] = job
            self._persist(job)
            return job
        except BaseException:
            for fd, _, _ in sources:
                os.close(fd)
            for fd, _ in secret_mounts:
                os.close(fd)
            if environment_fd is not None:
                os.close(environment_fd)
            if root_fd is not None:
                os.close(root_fd)
            if seccomp is not None:
                seccomp.close()
            if group is not None:
                group.cleanup()
            with self._lock:
                self._jobs.pop(identity, None)
            shutil.rmtree(directory)
            os.close(lease)
            raise

    def prepare(self, request):
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimePolicyError("runtime_closed", "This runtime has shut down.")
            if self.image is None:
                raise RuntimePolicyError("runtime_not_configured", "No approved runtime image is configured.")
            report = self.gateway_preflight(request.policy, request.gateway.route) if request.gateway is not None else self.preflight(request.policy)
            if not report.ready:
                raise RuntimePolicyError("isolation_unavailable", "Required native guarantees are unavailable; no workload was launched.")
            return self._allocate(request).token

    def _get(self, handle):
        if (not isinstance(handle, (ExecutionHandle, PreparedExecution))
                or not isinstance(handle.instance_id, str)):
            raise RuntimePolicyError("invalid_handle", "Expected a native execution handle.")
        with self._lock:
            job = self._jobs.get(handle.instance_id)
        if job is None or handle.provider != self.provider or job.token.policy_digest != handle.policy_digest:
            raise RuntimePolicyError("unknown_instance", "This backend does not own the execution instance.")
        return job

    def start(self, prepared):
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimePolicyError("runtime_closed", "This runtime has shut down.")
            job = self._get(prepared)
            with job.stopping:
                if job.state != "prepared":
                    raise RuntimePolicyError("prepared_execution_consumed", "Prepared executions can only be started once.")
                return self._launch(job)

    def _launch(self, job):
        binary = self._binary()
        job.state = "launching"
        self._persist(job)
        read_fd, status_fd = os.pipe()
        command = [binary, "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
                   "--unshare-uts", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
                   "--ro-bind-fd", str(job.fds[0]), "/", "--proc", "/proc", "--dev", "/dev",
                   "--remount-ro", "/dev", "--remount-ro", "/proc", "--seccomp", str(job.seccomp.fileno()),
                   "--json-status-fd", str(status_fd), "--sync-fd", str(job.lease_fd),
                   "--chdir", job.request.working_directory]
        size = scratch_size(job.request.policy.policy.to_document()["resources"])
        if size is not None:
            command.extend(["--size", str(size), "--perms", "0700", "--tmpfs", "/tmp"])
        for fd, target, access in job.mounts:
            command.extend(["--bind-fd" if access == "read_write" else "--ro-bind-fd", str(fd), target])
        for fd, target in job.secret_mounts:
            command.extend(["--perms", "0400", "--ro-bind-data", str(fd), target])
        if job.environment_fd is not None:
            command.extend(["--args", str(job.environment_fd)])
        command.extend(["--"] + list(job.request.argv))
        if job.group is not None:
            script = 'printf "%s\\n" "$$" > ' + shlex.quote(str(job.group.path / "cgroup.procs")) + ' || exit 125; exec "$@"'
            command = ["/bin/sh", "-c", script, "qavector-bootstrap"] + command
        try:
            job.process = subprocess.Popen(command, stdin=subprocess.PIPE if job.gateway is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "LANG": "C"}, cwd="/",
                start_new_session=True, close_fds=True,
                pass_fds=tuple(job.fds + [job.seccomp.fileno(), status_fd, job.lease_fd]))
        except OSError:
            os.close(read_fd)
            job.state, job.reason = "failed", "launch_failed"
            job.done.set()
            self._persist(job)
            raise RuntimePolicyError("launch_failed", "Cannot start the native launcher.") from None
        finally:
            os.close(status_fd)
            job.close_preparation()
        job.started_at, job.state = time.time(), "starting"
        try:
            job.status_thread = threading.Thread(target=self._status, args=(job, read_fd), daemon=True)
            job.status_thread.start()
            redactions = job.secret_redactions
            stdout = job.process.stdout
            if job.gateway is not None:
                from .gateway import GatewayOutput
                stdout = GatewayOutput(job.process, job.gateway)
            job.captures = [LogCapture(stdout, job.directory / "stdout.log", redactions),
                            LogCapture(job.process.stderr, job.directory / "stderr.log", redactions)]
            job.secret_redactions = ()
            for capture in job.captures:
                capture.start()
            threading.Thread(target=self._watch, args=(job,), daemon=True).start()
        except Exception:
            if job.status_thread is None or job.status_thread.ident is None:
                os.close(read_fd)
            self._terminate(job, time.monotonic(), "supervision_failed")
            for stream in (job.process.stdout, job.process.stderr):
                if not any(one.stream is stream and one.ident is not None for one in job.captures):
                    stream.close()
            self._watch(job)
            raise RuntimePolicyError("supervision_failed", "Native supervisor could not start; workload was terminated.") from None
        return ExecutionHandle(job.token.instance_id, self.provider, job.token.policy_digest)

    @staticmethod
    def _pin_child(pid, parent):
        fd = os.pidfd_open(pid)
        try:
            fields = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) != parent:
                raise RuntimePolicyError("unexpected_process", "Process identity changed before supervision.")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _status(self, job, fd):
        try:
            with os.fdopen(fd, "rb") as stream:
                while True:
                    line = stream.readline(4097)
                    if not line:
                        return
                    if len(line) > 4096:
                        job.reason = job.reason or "invalid_launcher_status"
                        return
                    message = json.loads(line)
                    if "child-pid" in message:
                        pid = message["child-pid"]
                        if type(pid) is not int or pid <= 0:
                            raise ValueError()
                        job.init_pid = pid
                        try:
                            job.init_pidfd = self._pin_child(pid, job.process.pid)
                            job.init_identity = process_identity(pid)
                        except (ProcessLookupError, FileNotFoundError):
                            pass
                        job.state = "running"
                        self._persist(job)
                    if "exit-code" in message:
                        job.child_exit_reported = True
        except (OSError, ValueError, RuntimePolicyError):
            job.reason = job.reason or "launcher_status_unavailable"

    def _signal_payload(self, job):
        if job.init_pidfd is None or select.select([job.init_pidfd], [], [], 0)[0]:
            return
        path = Path("/proc/%d/task/%d/children" % (job.init_pid, job.init_pid))
        for text in (_read(path) or "").split():
            try:
                fd = self._pin_child(int(text), job.init_pid)
                try:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                finally:
                    os.close(fd)
            except (ProcessLookupError, FileNotFoundError):
                pass

    def _terminate(self, job, deadline, reason):
        if job.gateway is not None:
            job.gateway.close()
        with job.stopping:
            if job.process is None or (job.done.is_set() and job.process.poll() is not None
                    and (job.init_pidfd is None or select.select([job.init_pidfd], [], [], 0)[0])):
                return
            job.reason = job.reason or reason
            self._signal_payload(job)
            try:
                job.process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            if job.init_pidfd is not None:
                try:
                    signal.pidfd_send_signal(job.init_pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if job.group is not None:
                job.group.kill()
            if job.process.poll() is None:
                job.process.kill()
            job.process.wait(timeout=3)

    def _watch(self, job):
        deadline = None if job.request.timeout_seconds is None else time.monotonic() + job.request.timeout_seconds
        try:
            while job.process.poll() is None:
                job.sample()
                if deadline is not None and time.monotonic() >= deadline:
                    self._terminate(job, time.monotonic(), "timeout")
                    break
                time.sleep(0.05)
            if job.status_thread is not None and job.status_thread.ident is not None:
                job.status_thread.join(3)
                if job.status_thread.is_alive():
                    raise RuntimePolicyError("launcher_status_unavailable", "Launcher status channel did not close.")
            if job.init_pidfd is not None and not select.select([job.init_pidfd], [], [], 3)[0]:
                signal.pidfd_send_signal(job.init_pidfd, signal.SIGKILL)
                if not select.select([job.init_pidfd], [], [], 3)[0]:
                    raise RuntimePolicyError("descendants_still_running", "Native namespace did not terminate.")
            if job.gateway is not None:
                job.gateway.close()
            for capture in job.captures:
                if capture.ident is None:
                    continue
                capture.join(3)
                if capture.is_alive():
                    raise RuntimePolicyError("logs_still_open", "Descendants still hold output streams.")
            job.exit_code = job.process.returncode
            job.sample()
            if job.group and job.group.metrics().get("memory_events", {}).get("oom_kill", 0):
                job.reason = "memory_limit"
            job.reason = job.reason or ("exited" if job.child_exit_reported else "launch_failed")
            job.state = "exited"
        except (OSError, RuntimePolicyError, ValueError, subprocess.TimeoutExpired) as exc:
            job.reason = exc.code if isinstance(exc, RuntimePolicyError) else "supervision_failed"
            job.state = "failed"
            try:
                self._terminate(job, time.monotonic(), job.reason)
            except (OSError, RuntimePolicyError, subprocess.TimeoutExpired):
                job.reason = "termination_unconfirmed"
        finally:
            if job.gateway is not None:
                job.gateway.close()
                if job.process.stdin is not None:
                    job.process.stdin.close()
            job.ended_at = time.time()
            try:
                self._persist(job)
            except (OSError, RuntimePolicyError):
                job.reason = "artifact_write_failed"
            job.done.set()

    def stop(self, handle, deadline):
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline > time.monotonic() + 60):
            raise RuntimePolicyError("invalid_deadline", "Stop requires a finite monotonic deadline within 60 seconds.")
        job = self._get(handle)
        if job.state == "prepared":
            raise RuntimePolicyError("execution_not_started", "Prepared execution has not started.")
        self._terminate(job, deadline, "cancelled")
        if not job.done.wait(10):
            raise RuntimePolicyError("termination_timeout", "Native execution has not completed cleanup.")

    def wait(self, handle):
        job = self._get(handle)
        if job.state == "prepared":
            raise RuntimePolicyError("execution_not_started", "Prepared execution has not started.")
        job.done.wait()
        return self.inspect(handle)

    def inspect(self, handle):
        job = self._get(handle)
        return {"instance_id": job.token.instance_id, "provider": self.provider,
                "policy_digest": job.token.policy_digest, "runtime_root_identity": self.image.identity,
                "state": job.state, "exit_code": job.exit_code, "reason": job.reason,
                "created_at": job.created_at, "started_at": job.started_at, "ended_at": job.ended_at,
                "stdout_tail": job.captures[0].tail.decode("utf-8", "replace") if job.captures else "",
                "stderr_tail": job.captures[1].tail.decode("utf-8", "replace") if job.captures else "",
                "logs_truncated": any(one.truncated for one in job.captures),
                "log_error": any(one.error for one in job.captures),
                "secret_files": list(job.secret_locations),
                **({"gateway": job.gateway.summary()} if job.gateway is not None else {}),
                "scratch": {"target": "/tmp", "capacity_bytes": scratch_size(job.request.policy.policy.to_document()["resources"]),
                            "accounting": "allocated_data_pages", "persistent": False},
                "resource_history": Snapshot(job.samples).to_document(),
                "artifacts": [str(job.directory / name) for name in ("runtime.json", "stdout.log", "stderr.log")]}

    def _persist(self, job):
        metadata = self.inspect(job.token)
        metadata.update({"kind": "ExecutionInstance", "schema_version": 1,
            "recovery": {"owner": job.owner_identity, "init": job.init_identity,
                         "failed_before_spawn": job.process is None and job.reason == "launch_failed",
                         "cgroup_parent": str(Path(self.cgroup_parent)) if job.group is not None else None}})
        if self.launcher is not None:
            metadata["launcher_identity"] = self.launcher.identity
        metadata["effective_policy"] = job.request.policy.to_document()
        if self._launcher_metadata is not None:
            metadata["launcher"] = self._launcher_metadata
        metadata["environment_names"] = list(job.environment_names)
        _atomic(job.directory / "runtime.json", metadata)

    def cleanup(self, handle):
        job = self._get(handle)
        with job.stopping:
            self._cleanup(job)

    def _cleanup(self, job):
        if job.gateway is not None:
            job.gateway.close()
        if job.state == "cleaned":
            self._persist(job)
            return
        if job.process is not None and (not job.done.is_set() or job.process.poll() is None):
            raise RuntimePolicyError("execution_still_running", "Stop and wait before releasing runtime resources.")
        if (any(one.is_alive() for one in job.captures)
                or job.status_thread is not None and job.status_thread.is_alive()
                or job.init_pidfd is not None and not select.select([job.init_pidfd], [], [], 0)[0]):
            raise RuntimePolicyError("descendants_still_running", "Resources remain owned until descendants terminate.")
        if job.gateway is not None:
            job.gateway.release()
        job.close_preparation()
        job.secret_redactions = ()
        if job.group is not None:
            job.group.cleanup()
        if job.init_pidfd is not None:
            os.close(job.init_pidfd)
            job.init_pidfd = None
        if (job.directory / "root").exists():
            shutil.rmtree(job.directory / "root")
        if job.state == "prepared":
            job.reason, job.ended_at = "preparation_cancelled", time.time()
        job.state = "cleaned"
        try:
            self._persist(job)
        finally:
            job.done.set()
            if job.lease_fd is not None:
                os.close(job.lease_fd)
                job.lease_fd = None

    def recover(self):
        if self.state_directory is None:
            raise RuntimePolicyError("runtime_not_configured", "Recovery requires a private state directory.")
        self._state()
        report = {"recovered": [], "active": [], "retained": []}
        with state_lock(self.state_directory):
            for path in sorted(self.state_directory.iterdir()):
                if not INSTANCE_ID.fullmatch(path.name):
                    continue
                lease = None
                try:
                    if path.is_symlink() or not path.is_dir():
                        raise RuntimePolicyError("unsafe_instance_directory", "Instance directory cannot be a symlink.")
                    lease = lock_file(path / "instance.lock", nonblocking=True)
                    if recover_instance(path, self.cgroup_parent, provider=self.provider):
                        report["recovered"].append(path.name)
                except BlockingIOError:
                    report["active"].append(path.name)
                except (OSError, RuntimePolicyError) as exc:
                    report["retained"].append({"instance_id": path.name,
                        "reason": exc.code if isinstance(exc, RuntimePolicyError) else "recovery_io_error"})
                finally:
                    if lease is not None:
                        os.close(lease)
        return report

    def shutdown(self, deadline):
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline > time.monotonic() + 60):
            raise RuntimePolicyError("invalid_deadline", "Shutdown requires a finite monotonic deadline within 60 seconds.")
        with self._lifecycle_lock:
            self._closed = True
            failures = []
            for job in list(self._jobs.values()):
                if job.state == "cleaned":
                    continue
                try:
                    if job.process is not None:
                        self.stop(job.token, deadline)
                    self.cleanup(job.token)
                except (OSError, RuntimePolicyError) as exc:
                    failures.append(exc.code if isinstance(exc, RuntimePolicyError) else "shutdown_io_error")
            if failures:
                raise RuntimePolicyError("shutdown_incomplete", "Some instance resources remain owned: " + ", ".join(failures))


class BundledBubblewrapBackend(LinuxBackend):
    """Preferred application-distributed launcher; no system executable fallback."""


class SystemBubblewrapBackend(LinuxBackend):
    """Explicit provider for an operator-selected compatible system installation."""

    provider = "linux-system-isolated"
    launcher_source = "system"
