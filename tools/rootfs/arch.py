"""Arch build chroots and native hardware package source preparation."""
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request

from build_chroot import bind_devices, remove_pacman_sockets, stop_helpers
from rootfs.common import digest

ROOT = Path(__file__).resolve().parents[2]

def run(args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)

@contextmanager
def guest_environment(root):
    """Temporary build mounts; keep pacman's runtime security defaults intact."""
    mounted = []
    devices = ExitStack()
    try:
        for name, kind in (('proc', 'proc'), ('run', 'tmpfs')):
            path = root / name
            path.mkdir(exist_ok=True)
            run(['mount', '-t', kind, kind, path])
            mounted.append(path)
        (root / 'dev').mkdir(exist_ok=True)
        devices.enter_context(bind_devices(root, run))
        (root / 'dev/fd').symlink_to('/proc/self/fd')
        text = (root / 'etc/pacman.conf').read_text()
        (root / 'run/pacman-build.conf').write_text(
            re.sub(r'^\s*CheckSpace\s*$', '# CheckSpace (build chroot)', text, flags=re.M))
        help_text = subprocess.check_output(
            ['chroot', root, 'pacman', '-S', '--help'], text=True)
        sandbox_args = ['--disable-sandbox'] if '--disable-sandbox' in help_text else []
        def guest(args, **kwargs):
            if args[0] == 'pacman':
                # The build may run under QEMU or an older device kernel
                # without Landlock. Do not change the installed policy.
                args = [args[0], '--config', '/run/pacman-build.conf',
                        *sandbox_args, *args[1:]]
            # Package hooks can daemonize, change root or retain an fd into
            # /dev. Give each command its own process namespace so those
            # descendants cannot outlive the command and pin the build mounts.
            # systemd must still treat PID 1 in this namespace as a build chroot.
            return run(['unshare', '--pid', '--fork', '--kill-child',
                        '--mount-proc=' + str(root / 'proc'),
                        'chroot', root, '/usr/bin/env',
                        'SYSTEMD_OFFLINE=1', 'SYSTEMD_IN_CHROOT=1',
                        'PATH=/usr/bin:/usr/sbin:/bin:/sbin', *args], **kwargs)

        yield guest
    finally:
        subprocess.run(['chroot', root, 'gpgconf', '--homedir',
                        '/etc/pacman.d/gnupg', '--kill', 'all'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stop_helpers(root)
        # PID namespace teardown may SIGKILL a daemon before it unlinks its
        # sockets. Remove these temporary endpoints before packaging.
        remove_pacman_sockets(root)
        devices.close()
        for path in reversed(mounted):
            # Package hooks may mount devpts/shm below the temporary /dev.
            # Remove children first; do not lazily detach a busy build tree.
            try:
                run(['umount', '--recursive', path])
            except subprocess.CalledProcessError:
                subprocess.run(['findmnt', '--submounts', '--target', path],
                               check=False)
                raise

def download(url, path, expected):
    if not path.exists():
        partial = path.with_suffix(path.suffix + '.partial')
        with urllib.request.urlopen(url, timeout=60) as source, partial.open('wb') as target:
            shutil.copyfileobj(source, target)
        if digest(partial) != expected:
            raise ValueError('Download checksum mismatch: ' + url)
        partial.replace(path)
    if digest(path) != expected:
        raise ValueError('Cached input checksum mismatch: ' + str(path))

def sensor_sources(output, cfg):
    source = ROOT / cfg['source']
    commit = subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], text=True).strip()
    if commit != cfg['commit'] or subprocess.check_output(
            ['git', '-C', source, 'status', '--porcelain'], text=True):
        raise ValueError('Run ./build.sh sources to obtain the fixed sensor source')
    shutil.copytree(source, output, ignore=shutil.ignore_patterns('.git'))
    rows = []
    for name in cfg['patches']:
        run(['patch', '--batch', '--forward', '-p1', '-i', ROOT / name], cwd=output)
        rows.append({'path': name, 'sha256': digest(ROOT / name)})
    (output / 'SOURCE').write_text(json.dumps(
        {'source_url': cfg['source_url'], 'source_commit': commit, 'patches': rows}, indent=2) + '\n')

def sensor_recipes(output, source):
    for name in ('fastrpc', 'libssc', 'iio-sensor-proxy'):
        recipe = output / name
        shutil.copytree(ROOT / 'linux/bsp/arch' / name, recipe)
        if name == 'fastrpc':
            continue
        if name == 'libssc':
            filename, pool, checksum = (
                'libssc_0.4.4.orig.tar.gz', 'libs/libssc',
                '716d6bd6b34d2d753060c6b54c9a87e34fae75b724c763bf9ef487efa3621587')
        else:
            filename, pool, checksum = (
                'iio-sensor-proxy_3.9.orig.tar.bz2', 'i/iio-sensor-proxy',
                '19a4ae99b1b8af603fc789841025f9747bf445418d69ec7389e75adb6c8186bf')
        download('https://deb.debian.org/debian/pool/main/' + pool + '/' + filename,
                 recipe / filename, checksum)
        for patch in (source / 'patches' / name).glob('*.patch'):
            shutil.copy2(patch, recipe / patch.name)
