"""Stop helpers left inside a finished build root before unmounting its /dev."""
import os
from contextlib import contextmanager
from pathlib import Path
import select
import signal
import stat
import sys
import tempfile
import time


@contextmanager
def bind_devices(root, run, tty=False):
    """Expose only basic character devices, including in rootless Podman."""
    # A new tmpfs in a user namespace cannot host usable device nodes, even
    # through individual bind mounts. A private directory in inherited /dev
    # keeps that mount's device semantics without exposing its other devices.
    with tempfile.TemporaryDirectory(prefix='sunuefi-build-', dir='/dev') as folder:
        os.chmod(folder, 0o755)
        mounted = []
        dev = Path(root) / 'dev'
        try:
            run(['mount', '--bind', folder, dev])
            mounted.append(dev)
            for name in ('null', 'zero', 'random', 'urandom', *(('tty',) if tty else ())):
                source = Path('/dev') / name
                if not stat.S_ISCHR(source.stat().st_mode):
                    raise ValueError('Expected a character device: ' + str(source))
                target = dev / name
                target.touch()
                run(['mount', '--bind', source, target])
                mounted.append(target)
            yield
        finally:
            for path in reversed(mounted):
                run(['umount', '--recursive', path])


def remove_pacman_sockets(root):
    """Remove dead IPC endpoints, not pacman's keys, trust or package state."""
    directory = Path(root) / 'etc/pacman.d/gnupg'
    for path in directory.glob('S.*'):
        if stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()


def stop_helpers(root):
    """Terminate only processes whose chroot is this build's root directory."""
    root = Path(root).resolve()
    if root == Path('/'):
        raise ValueError('Refusing to stop processes in the host root')
    pending = {}
    poller = select.poll()
    try:
        for proc in Path('/proc').glob('[0-9]*'):
            fd = None
            try:
                if not (proc / 'root').samefile(root):
                    continue
                fd = os.pidfd_open(int(proc.name))
                # The PID may have exited or been reused while opening it.
                if not (proc / 'root').samefile(root):
                    os.close(fd)
                    continue
                print(f'Stopping build helper {proc.name} in {root}', file=sys.stderr)
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                poller.register(fd, select.POLLIN)
                pending[fd] = proc.name
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                if fd is not None:
                    os.close(fd)
        # GnuPG's --kill command requests shutdown but need not wait for exit.
        # pidfds let us wait without confusing zombies or reused PIDs.
        for timeout, terminate in ((2000, False), (1000, True)):
            if not pending:
                break
            if terminate:
                for fd in pending:
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + timeout / 1000
            while pending:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                for fd, _ in poller.poll(remaining):
                    poller.unregister(fd)
                    os.close(fd)
                    del pending[fd]
                if time.monotonic() >= deadline:
                    break
        if pending:
            raise RuntimeError(f'Build helpers did not exit: {list(pending.values())}')
    finally:
        for fd in pending:
            os.close(fd)
