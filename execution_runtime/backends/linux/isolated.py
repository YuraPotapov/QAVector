"""A network of its own for one restricted program, with chosen ports published.

bwrap gives the program a network namespace of its own (``--unshare-net``), so
its ports cannot clash with anything: two services can both listen on 8069.
pasta - bundled, GPL-2.0-or-later, see licenses/passt - connects that namespace
to this computer, and only as configured:

- **publish**: a port of the program on an address of this computer
  (``127.0.0.1:18069`` -> its ``8069``), at start or later through pesto,
  without restarting it. ``auto`` takes a free port when it is bound.
- **host_loopback**: the program reaches the services on this computer's
  loopback - a database, another service's published port - at their usual
  ``127.0.0.1`` addresses.
- **outbound**: the program reaches the internet, names resolving through the
  host's resolver.

Neither of the last two: pasta runs with no interface in the namespace at all
(``--splice-only``) and only the published ports go in.

How the pieces meet, found by prototype: bwrap starts blocked (``--block-fd``)
and says its child's pid (``--info-fd``). The child's network namespace is owned
by an intermediate user namespace bwrap made, not the child's own, so pasta is
given that owner - from ``NS_GET_USERNS`` on the network namespace - and the
child's ``/proc/<pid>/ns/net``. pasta daemonizes once the network is set up,
which is when the program is let go. It exits by itself when the namespace goes,
so nothing outlives the program however it ends.

The state file - the control socket and what is published - is how the GUI and
``--network-ports`` reach a running program; it is written atomically and
removed when the program ends.
"""

import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time

from ...errors import RuntimePolicyError

NS_GET_USERNS = 0xb701
#: What the program asks for names; pasta forwards it to the host's resolver.
DNS_ADDRESS = "169.254.1.53"
#: How many free ports ``auto`` tries before saying none could be had.
AUTO_TRIES = 5
#: How long the sandbox may take to say its pid.
START_TIMEOUT = 15.0
BUSY = re.compile(r"Address already in use|already in use|EADDRINUSE", re.I)


def resolver_target():
    """Where /etc/resolv.conf really is: on Ubuntu a link into /run, which
    bwrap cannot mount over, so the program's own copy goes at its target."""
    return os.path.realpath("/etc/resolv.conf")


def _fail(code, message):
    raise RuntimePolicyError(code, message)


def free_port(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((address, 0))
        return probe.getsockname()[1]


def spec(publication):
    """pasta's ``-t`` for one publication with its host port decided."""
    return "%s/%d:%d" % (publication["host_ip"], publication["host_port"], publication["internal_port"])


def pasta_options(owner_path, netns_path, control, pidfile, effective, publications):
    options = ["--config-net", "--quiet", "--userns", owner_path, "--netns", netns_path,
               "-c", control, "-P", pidfile]
    # One -t each: pasta reads an address as applying to a whole -t list.
    for one in publications:
        options += ["-t", spec(one)]
    if not publications:
        options += ["-t", "none"]
    options += ["-u", "none",
               # Published ports reach a program listening on its own
               # 127.0.0.1 as well as one listening on every address.
               "--host-lo-to-ns-lo"]
    if effective.get("host_loopback"):
        options += ["-T", "auto", "-U", "auto"]
    else:
        options += ["-T", "none", "-U", "none", "--no-map-gw"]
    if effective.get("outbound"):
        options += ["--dns-forward", DNS_ADDRESS]
    else:
        options.append("--splice-only")
    return options


class Network:
    """One program's isolated network: start it, publish on it, end it."""

    def __init__(self, pasta, pesto, effective, state_path=""):
        self.pasta, self.pesto, self.effective = pasta, pesto, effective
        if any(one.get("public") for one in effective.get("publish") or []) and not effective.get("outbound"):
            _fail("public_needs_interface",
                  "Publishing to other computers needs the isolated network's interface, which comes with "
                  "Internet; turn Internet on, or publish on 127.0.0.1.")
        self.state_path = state_path
        self.folder = tempfile.mkdtemp(prefix="qavector-net-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
        self.control = os.path.join(self.folder, "pasta.sock")
        self.pidfile = os.path.join(self.folder, "pasta.pid")
        self.publications = []
        self.process = None

    # -- starting -------------------------------------------------------------------
    def start(self, argv, env, cwd, pass_fds, at):
        """Start the sandbox ``argv`` (bwrap at ``argv[at]``) blocked, connect its
        network, then let it go. Returns the sandbox process."""
        read_info, write_info = os.pipe()
        read_block, write_block = os.pipe()
        argv = list(argv[:at + 1]) + ["--info-fd", str(write_info), "--block-fd", str(read_block)] + list(argv[at + 1:])
        try:
            self.process = subprocess.Popen(argv, cwd=cwd, env=env, pass_fds=tuple(pass_fds) + (write_info, read_block))
        finally:
            os.close(write_info)
            os.close(read_block)
        child = None
        try:
            child = self._child(read_info)
            self._connect(child)
            os.write(write_block, b"1")
        except BaseException:
            # The child first, while it still waits: released before it knew
            # to die with bwrap, it would go on and run without its network.
            if child is not None:
                try:
                    os.kill(child, 9)
                except OSError:
                    pass
            self.process.kill()
            self.process.wait()
            self.close()
            raise
        finally:
            os.close(write_block)
            os.close(read_info)
        self._save()
        return self.process

    def _child(self, read_info):
        raw, deadline = b"", time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            chunk = os.read(read_info, 4096)
            if not chunk:
                break
            raw += chunk
            try:
                return int(json.loads(raw.decode("utf-8"))["child-pid"])
            except (ValueError, KeyError):
                continue
        _fail("isolated_network_start", "The sandbox did not start, so its network could not be set up.")

    def _connect(self, child):
        """Run pasta into the child's network; retry ``auto`` ports that were taken."""
        netns = os.open("/proc/%d/ns/net" % child, os.O_RDONLY)
        try:
            owner = fcntl.ioctl(netns, NS_GET_USERNS)
        finally:
            os.close(netns)
        try:
            wanted = list(self.effective.get("publish") or [])
            for attempt in range(AUTO_TRIES):
                chosen = [dict(one, host_port=free_port(one["host_ip"]) if one["host_port"] == "auto" else one["host_port"],
                               auto=one["host_port"] == "auto") for one in wanted]
                done = subprocess.run(
                    [self.pasta] + pasta_options("/proc/%d/fd/%d" % (os.getpid(), owner), "/proc/%d/ns/net" % child,
                                                 self.control, self.pidfile, self.effective, chosen),
                    capture_output=True, text=True, timeout=30)
                if done.returncode == 0:
                    self.publications = [_record(one) for one in chosen]
                    return
                said = (done.stderr or done.stdout or "").strip()
                if not (BUSY.search(said) and any(one["auto"] for one in chosen)) or attempt == AUTO_TRIES - 1:
                    _fail("isolated_network_failed", "The isolated network could not be set up: %s" % (said or "pasta failed"))
        finally:
            os.close(owner)

    # -- while it runs ------------------------------------------------------------------
    def publish(self, publication):
        entry = publish(self.pesto, self.control, self.publications, publication,
                        bool(self.effective.get("outbound")))
        self._save()
        return entry

    def unpublish(self, publication):
        entry = unpublish(self.pesto, self.control, self.publications, publication)
        self._save()
        return entry

    def _save(self):
        if self.state_path:
            save_state(self.state_path, {"pid": os.getpid(), "control": self.control, "pesto": self.pesto,
                                         "folder": self.folder, "publications": self.publications,
                                         "outbound": bool(self.effective.get("outbound")),
                                         "host_loopback": bool(self.effective.get("host_loopback"))})

    def close(self):
        """After the program ends: pasta goes with its namespace; tidy the rest."""
        try:
            with open(self.pidfile) as handle:
                pid = int(handle.read().strip())
            os.kill(pid, 15)
        except (OSError, ValueError):
            pass
        if self.state_path:
            try:
                with open(self.state_path) as handle:
                    mine = json.load(handle).get("pid") == os.getpid()
                if mine:
                    os.unlink(self.state_path)
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.folder, ignore_errors=True)


def _record(chosen):
    entry = {"internal_port": chosen["internal_port"], "host_port": chosen["host_port"],
             "host_ip": chosen["host_ip"], "since": int(time.time())}
    if chosen.get("auto"):
        entry["auto"] = True
    if chosen.get("public"):
        entry["public"] = True
    return entry


def _pesto(pesto, control, *arguments):
    done = subprocess.run([pesto] + list(arguments) + [control], capture_output=True, text=True, timeout=15)
    return done.returncode, (done.stderr or done.stdout or "").strip()


def publish(pesto, control, publications, publication, outbound=False):
    """Add one publication to a running program; returns it as bound."""
    from ...restrictions import publications as check
    publication = check([publication])[0]
    if publication.get("public") and not outbound:
        _fail("public_needs_interface", "Publishing to other computers needs Internet on for this isolated network.")
    for attempt in range(AUTO_TRIES):
        chosen = dict(publication, auto=publication["host_port"] == "auto",
                      host_port=free_port(publication["host_ip"]) if publication["host_port"] == "auto"
                      else publication["host_port"])
        if any(one["host_ip"] == chosen["host_ip"] and one["host_port"] == chosen["host_port"] for one in publications):
            _fail("port_already_published", "%s:%d is already published." % (chosen["host_ip"], chosen["host_port"]))
        code, said = _pesto(pesto, control, "-A", "-t", spec(chosen))
        if code == 0:
            entry = _record(chosen)
            publications.append(entry)
            return entry
        if not (chosen["auto"] and BUSY.search(said)) or attempt == AUTO_TRIES - 1:
            _fail("publish_failed", "Could not publish %s:%s: %s" % (chosen["host_ip"], chosen["host_port"], said or "pesto failed"))


def unpublish(pesto, control, publications, publication):
    """Take one publication away; the program keeps running. Idempotent."""
    match = [one for one in publications if one["host_ip"] == publication.get("host_ip", "127.0.0.1")
             and one["host_port"] == publication.get("host_port")]
    if not match:
        return None
    code, said = _pesto(pesto, control, "-D", "-t", spec(match[0]))
    if code != 0:
        _fail("unpublish_failed", "Could not unpublish %s:%d: %s" % (match[0]["host_ip"], match[0]["host_port"], said))
    publications.remove(match[0])
    return match[0]


# -- the state file the GUI and --network-ports read ---------------------------------------
def save_state(path, state):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".network-", dir=folder)
    with os.fdopen(handle, "w") as stream:
        json.dump(state, stream, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_state(path):
    """The state of a running isolated program, or raise when it is not running."""
    try:
        with open(path) as handle:
            state = json.load(handle)
        os.kill(int(state["pid"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        _fail("not_running", "That service is not running with an isolated network.")
    return state


def change(path, operation, publication=None):
    """``list``, ``add`` or ``remove`` on a running program, under a lock."""
    lock = open(path + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(path)
        result = None
        if operation == "add":
            result = publish(state["pesto"], state["control"], state["publications"], publication or {},
                             outbound=state.get("outbound", False))
        elif operation == "remove":
            result = unpublish(state["pesto"], state["control"], state["publications"], publication or {})
        elif operation != "list":
            _fail("invalid_request", "Use list, add or remove.")
        if operation != "list":
            save_state(path, state)
        return {"publications": state["publications"], "changed": result}
    finally:
        lock.close()
