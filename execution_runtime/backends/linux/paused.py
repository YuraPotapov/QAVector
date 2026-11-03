"""Hold a namespace init and descendants using pinned process identities."""

import os
from pathlib import Path
import select
import signal
import time

from ...errors import RuntimePolicyError


class FrozenTree:
    def __init__(self, pid, pidfd, pin_child):
        self.root, self.pin_child = pid, pin_child
        self.fds = {pid: os.dup(pidfd)}

    def hold(self):
        until = time.monotonic() + 2
        stopped = set()
        try:
            # Stop a parent before enumerating children (including children
            # owned by other threads). Stopped parents cannot fork new work.
            for _ in range(4):
                queue = [self.root]
                while queue:
                    pid = queue.pop()
                    fd = self.fds[pid]
                    if select.select([fd], [], [], 0)[0]:
                        continue
                    if pid not in stopped:
                        signal.pidfd_send_signal(fd, signal.SIGSTOP)
                        while True:
                            if select.select([fd], [], [], 0)[0]:
                                break
                            state = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[0]
                            if state in ('T', 't', 'Z'):
                                break
                            if time.monotonic() >= until:
                                raise RuntimePolicyError('pause_unconfirmed', 'Native process did not acknowledge Pause.')
                            time.sleep(0.002)
                        stopped.add(pid)
                    for task in Path('/proc/%s/task' % pid).glob('*'):
                        try:
                            children = (task / 'children').read_text().split()
                        except FileNotFoundError:
                            continue
                        for child in map(int, children):
                            if child in self.fds:
                                queue.append(child)
                                continue
                            if len(self.fds) >= 4096 or time.monotonic() >= until:
                                raise RuntimePolicyError('pause_unconfirmed', 'Native pause exceeded its bound.')
                            try:
                                child_fd = self.pin_child(child, pid)
                                if select.select([fd], [], [], 0)[0]:
                                    os.close(child_fd)
                                    continue
                                self.fds[child] = child_fd
                                queue.append(child)
                            except (ProcessLookupError, FileNotFoundError):
                                pass
        except BaseException:
            self.release()
            raise

    def release(self):
        try:
            # Children first; the namespace init can reap them once released.
            for fd in reversed(list(self.fds.values())):
                try:
                    signal.pidfd_send_signal(fd, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        finally:
            self.close()

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()
