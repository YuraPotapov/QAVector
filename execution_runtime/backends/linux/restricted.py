"""Programs held to their project folder, on the host's own tools.

The image mode of this backend copies an approved file list into an empty root.
This mode mirrors the host instead. The system folders are bound read-only at
their own paths, so ``python3``, ``git`` and the libraries they load are the
ones the program would have used anyway; the project folder, and any folder
listed beside it, are bound at their own paths too, so commands, working
directories and config files written against them keep working. What the
program does not get is everything else: the home folder is an empty private
one, other projects are not there, ``/tmp`` and ``/run`` are its own, and
QAVector's control data - secrets, saved logins, browser profiles - is masked
even inside a folder that was granted.

Network off is a network namespace with nothing in it. Network on is the host's
own, which is what a service listening on a port or a step talking to a local
database needs; the resolver and the PostgreSQL socket directory are then
mounted read-only so names resolve and ``psql`` finds its socket.

Limits go through ``systemd-run --user --scope``: the user's service manager
creates a cgroup, applies MemoryMax/TasksMax/CPUQuota before the program runs,
and removes it when the program ends. A controller the user's manager does not
have - cpu, on a stock Ubuntu - would be accepted and silently ignored, so each
limit's availability is probed by reading the applied value back, and a limit
that cannot be enforced refuses the launch instead of running without it.
"""

import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import threading

from ...errors import RuntimePolicyError
from ...restrictions import unrestricted, within
from .seccomp import filter_file

#: Bound read-only at their own paths (a symlink is recreated as a symlink).
SYSTEM_PATHS = ("/usr", "/etc", "/opt", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
#: Bound read-only only while the network is on.
NETWORK_PATHS = ("/run/systemd/resolve", "/run/postgresql")

PROBE_MEMORY = 64 * 1024 * 1024
PROBE_TASKS = 32

_CACHE = {}
_CACHE_LOCK = threading.Lock()


class Prepared:
    """How to start one restricted program; the caller owns the process."""

    def __init__(self, argv, env, cwd, pass_fds=(), resources=()):
        self.argv, self.env, self.cwd = list(argv), dict(env), cwd
        self.pass_fds = tuple(pass_fds)
        self._resources = list(resources)

    def close(self):
        """Release what only the launch needed (the seccomp filter file)."""
        for one in self._resources:
            try:
                one.close()
            except OSError:
                pass
        self._resources = []


def default_protected():
    """QAVector's own control data, never visible to a restricted program.

    Secrets and saved logins, and also everything that decides what a program
    may do - the cycle files and services.json carry the restrictions
    themselves, and a program that could rewrite them would choose its own for
    the next run.
    """
    import runtime_paths
    roots = [runtime_paths.user_data_root(), os.path.join(os.path.expanduser("~"), runtime_paths.USER_DIR_NAME)]
    names = ("cyclesecrets.json", "cyclesecrets.key", "users.json", "user_sessions",
             "execution-deployments", "execution-environments", "runtime-state",
             "logsources.json", "services.json", "cycles", "cycleprojects.json",
             "cyclepresets.json", "cyclememory.json", "cycle-runs")
    return tuple(dict.fromkeys(os.path.join(root, one) for root in roots for one in names))


def private_paths(home):
    """Hidden when they sit inside a granted folder, unless granted themselves."""
    return tuple(os.path.join(home, one) for one in (".ssh", ".gnupg", ".aws", ".netrc", ".docker"))


class RestrictedLauncher:
    def __init__(self, bundle_directory=None, protected=None, home=None, launcher_path=None):
        self.bundle_directory = bundle_directory
        self.protected = tuple(default_protected() if protected is None else protected)
        self.home = home or os.path.expanduser("~")
        #: Tests point this at a verified bwrap explicitly; the application
        #: always uses the bundled, integrity-checked one.
        self.launcher_path = launcher_path

    # -- what this computer can enforce ------------------------------------------------
    def _launcher(self):
        if self.launcher_path is not None:
            return {"source": "explicit", "path": self.launcher_path}
        from .bundled import BundledLauncher
        return BundledLauncher(self.bundle_directory).executable()

    def capabilities(self):
        """What can be enforced here, each with a reason when it cannot.

        Probed once per process and launcher: a probe starts real sandboxes,
        and a GUI asking on every keystroke must not pay for that each time.
        """
        key = (self.bundle_directory, self.launcher_path)
        with _CACHE_LOCK:
            if key not in _CACHE:
                _CACHE[key] = self._probe()
            return _CACHE[key]

    def _probe(self):
        result = {"platform": "linux", "available": False, "reason": "", "launcher": {},
                  "limits": {name: {"available": False, "reason": ""} for name in ("memory_mb", "processes", "cpu_cores")}}
        try:
            if platform.system() != "Linux" or platform.machine() != "x86_64":
                raise RuntimePolicyError("restrictions_unsupported", "Restrictions currently need Linux on x86_64.")
            launcher = self._launcher()
            result["launcher"] = {key: launcher[key] for key in ("source", "path", "version") if key in launcher}
            self._isolation_probe(launcher["path"])
            result["available"] = True
        except RuntimePolicyError as exc:
            result["reason"] = str(exc)
        except (OSError, subprocess.SubprocessError):
            result["reason"] = "This computer cannot start a restricted program (user namespaces may be disabled)."
        result["limits"] = self._limits_probe()
        return result

    def _isolation_probe(self, binary):
        effective = {"restricted": True, "network": "off", "files": "none", "folders": [], "limits": {},
                     "project_folder": ""}
        prepared = self._sandbox(binary, effective, ["/bin/sh", "-c",
                                 "cat /proc/self/status; readlink /proc/self/ns/net"], "/", {"PATH": "/usr/bin:/bin"})
        try:
            done = subprocess.run(prepared.argv, env=prepared.env, cwd=prepared.cwd, pass_fds=prepared.pass_fds,
                                  capture_output=True, timeout=10)
        finally:
            prepared.close()
        output = done.stdout.decode("utf-8", "replace")
        if (done.returncode != 0 or "NoNewPrivs:\t1" not in output or "Seccomp:\t2" not in output
                or "CapEff:\t0000000000000000" not in output or os.readlink("/proc/self/ns/net") in output):
            raise RuntimePolicyError("restrictions_probe_failed",
                "A test sandbox did not come up isolated; restrictions cannot be enforced here.")

    def _limits_probe(self):
        found = {name: {"available": False, "reason": ""} for name in ("memory_mb", "processes", "cpu_cores")}
        session = ("Limits need a desktop session with a user service manager (systemd --user); "
                   "none was found for this program.")
        if shutil.which("systemd-run") is None or not os.environ.get("XDG_RUNTIME_DIR"):
            for one in found.values():
                one["reason"] = session
            return found
        script = ('c=$(sed -n "s/^0:://p" /proc/self/cgroup); '
                  'for f in memory.max memory.swap.max pids.max cpu.max; do echo "$f=$(cat /sys/fs/cgroup$c/$f 2>/dev/null)"; done')
        try:
            done = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect",
                                   "-p", "MemoryMax=%d" % PROBE_MEMORY, "-p", "MemorySwapMax=0",
                                   "-p", "TasksMax=%d" % PROBE_TASKS, "-p", "CPUQuota=50%",
                                   "--", "/bin/sh", "-c", script], capture_output=True, timeout=10)
            values = dict(line.split("=", 1) for line in done.stdout.decode("ascii", "replace").splitlines() if "=" in line)
        except (OSError, subprocess.SubprocessError, ValueError):
            values, done = {}, None
        if done is None or done.returncode != 0:
            for one in found.values():
                one["reason"] = session
            return found
        if values.get("memory.max") == str(PROBE_MEMORY) and values.get("memory.swap.max") == "0":
            found["memory_mb"]["available"] = True
        else:
            found["memory_mb"]["reason"] = "Your session's service manager does not control memory."
        if values.get("pids.max") == str(PROBE_TASKS):
            found["processes"]["available"] = True
        else:
            found["processes"]["reason"] = "Your session's service manager does not control process counts."
        if values.get("cpu.max", "").startswith("50000 "):
            found["cpu_cores"]["available"] = True
        else:
            found["cpu_cores"]["reason"] = ("CPU limits need the cpu controller delegated to your user session - "
                                            "a one-time administrator setting (Delegate=cpu for user@.service).")
        return found

    # -- one launch -------------------------------------------------------------------
    def prepare(self, effective, argv, cwd, env, extra_folders=()):
        """Turn a resolved restriction into an argv the caller can start.

        ``extra_folders`` are folders the adapter itself needs the program to
        see - the interpreter it was told to run, a step's own output
        directory - as ``{"path", "access"}``. Nothing here starts the program.
        """
        if not argv or any(not isinstance(one, str) or "\0" in one for one in argv):
            raise RuntimePolicyError("invalid_request", "Expected a command to run.")
        if unrestricted(effective):
            return Prepared(argv, env, cwd)
        capabilities = self.capabilities()
        sandbox = effective["network"] == "off" or effective["files"] != "all"
        if sandbox and not capabilities["available"]:
            raise RuntimePolicyError("restrictions_unavailable", capabilities["reason"] or
                                     "Restrictions cannot be enforced on this computer.")
        prefix = []
        limits = effective.get("limits") or {}
        if limits:
            for key in limits:
                state = capabilities["limits"][key]
                if not state["available"]:
                    raise RuntimePolicyError("limit_unavailable", state["reason"], "limits." + key)
            prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
            if "memory_mb" in limits:
                prefix += ["-p", "MemoryMax=%dM" % limits["memory_mb"], "-p", "MemorySwapMax=0"]
            if "processes" in limits:
                prefix += ["-p", "TasksMax=%d" % limits["processes"]]
            if "cpu_cores" in limits:
                prefix += ["-p", "CPUQuota=%d%%" % max(1, round(limits["cpu_cores"] * 100))]
            prefix.append("--")
        environment = dict(env)
        if prefix:
            # systemd-run talks to the user's manager through these; the
            # program cannot use them - /run is its own.
            for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                if name not in environment and os.environ.get(name):
                    environment[name] = os.environ[name]
        if not sandbox:
            return Prepared(prefix + list(argv), environment, cwd)
        prepared = self._sandbox(capabilities["launcher"]["path"], effective, argv, cwd, environment, extra_folders)
        prepared.argv = prefix + prepared.argv
        return prepared

    def _sandbox(self, binary, effective, argv, cwd, env, extra_folders=()):
        command = [binary, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
                   "--unshare-cgroup-try", "--die-with-parent", "--cap-drop", "ALL"]
        if effective["network"] == "off":
            command.append("--unshare-net")
        resources = []
        try:
            if effective["files"] == "all":
                command += ["--bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--chdir", cwd]
            else:
                command += self._mirror(effective, extra_folders, resources)
                command += ["--chdir", self._workdir(effective, cwd, extra_folders)]
            seccomp = filter_file(restricted=True)
            resources.append(seccomp)
            for one in resources:
                os.set_inheritable(one.fileno(), True)
        except BaseException:
            for one in resources:
                one.close()
            raise
        command += ["--seccomp", str(seccomp.fileno()), "--"] + list(argv)
        return Prepared(command, env, "/", tuple(one.fileno() for one in resources), resources)

    def _visible(self, effective, extra_folders):
        """(path, access) of everything granted, parents before children."""
        granted = []
        project = effective.get("project_folder") or ""
        if project and effective["files"] in ("read_write", "read_only"):
            granted.append((project, effective["files"]))
        granted += [(one["path"], one["access"]) for one in effective.get("folders", [])]
        granted += [(one["path"], one["access"]) for one in extra_folders if one["access"] != "copy"]
        return sorted(dict(granted).items(), key=lambda one: (len(PurePosixPath(one[0]).parts), one[0]))

    def _workdir(self, effective, cwd, extra_folders):
        if not cwd or cwd == "/":
            return "/"
        if cwd == self.home or any(within(path, cwd) for path, _ in self._visible(effective, extra_folders)):
            return cwd
        project = effective.get("project_folder") or ""
        if project and effective["files"] == "none":
            raise RuntimePolicyError("working_directory_hidden",
                "File access is off, so the program cannot run in %s." % cwd)
        raise RuntimePolicyError("working_directory_hidden",
            "%s is outside the project folder and the folders this program may see." % cwd)

    def _mirror(self, effective, extra_folders, resources):
        args = []
        for path in SYSTEM_PATHS:
            if os.path.islink(path):
                args += ["--symlink", os.readlink(path), path]
            elif os.path.isdir(path):
                args += ["--ro-bind", path, path]
        args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/var",
                 "--tmpfs", "/var/tmp", "--tmpfs", "/run", "--symlink", "../run", "/var/run"]
        if effective["network"] == "on":
            for path in NETWORK_PATHS:
                if os.path.isdir(path):
                    args += ["--ro-bind", path, path]
        if self.home and self.home != "/" and not os.path.islink(self.home):
            args += ["--tmpfs", self.home]
        visible = self._visible(effective, extra_folders)
        project = effective.get("project_folder") or ""
        for path, access in visible:
            if path == project and not os.path.isdir(path):
                raise RuntimePolicyError("project_folder_missing", "The project folder %s does not exist." % path)
            args += ["--bind-try" if access == "read_write" else "--ro-bind-try", path, path]
        for one in extra_folders:
            if one["access"] == "copy" and os.path.isfile(one["path"]):
                # A private, writable copy: the program may rewrite it, and
                # nothing it writes reaches the original.
                handle = open(one["path"], "rb")
                resources.append(handle)
                args += ["--perms", "0600", "--file", str(handle.fileno()), one["path"]]
        sources = [path for path, _ in visible]
        masked = []
        for path in self.protected:
            # QAVector's own data stays hidden even inside an explicitly granted folder.
            if any(within(source, path) for source in sources):
                args += self._mask(path)
                masked.append(path)
        for path in private_paths(self.home):
            if any(within(source, path) and source != path for source in sources) and not any(within(path, source) for source in sources):
                args += self._mask(path)
                masked.append(path)
        # What was granted inside something masked - a step's own output folder
        # under the run directory - is put back on top of the mask.
        for path, access in visible:
            if any(within(hidden, path) and hidden != path for hidden in masked):
                args += ["--bind-try" if access == "read_write" else "--ro-bind-try", path, path]
        return args

    @staticmethod
    def _mask(path):
        if os.path.isdir(path) and not os.path.islink(path):
            return ["--tmpfs", path]
        if os.path.lexists(path):
            return ["--ro-bind", "/dev/null", path]
        return []


def launcher(bundle_directory=None, **kwargs):
    """The launcher the application uses; ``QAVECTOR_RUNTIME_DIR`` points a
    source checkout at a built runtime without copying it."""
    directory = bundle_directory or os.environ.get("QAVECTOR_RUNTIME_DIR") or None
    return RestrictedLauncher(directory, **kwargs)


def reset_capabilities():
    """Forget probed capabilities (tests, or after the user fixed something)."""
    with _CACHE_LOCK:
        _CACHE.clear()
