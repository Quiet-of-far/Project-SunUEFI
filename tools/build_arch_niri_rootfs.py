#!/usr/bin/env python3
"""Build a Piano Arch Linux ARM rootfs with niri and the shared hardware stack."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import assemble_rootfs
import build_arch_ffmpeg
import build_arch_package
from rootfs import common as release
import stage_arch_piano
from provision_piano_ssh import public_key
from rootfs.arch import guest_environment
from build_chroot import bind_devices
from rootfs.arch import sensor_recipes, sensor_sources

ROOT = Path(__file__).resolve().parents[1]


def debug_identity(device_id, key_file):
    """Keep network identity stable across reinstalls of this device's image."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', device_id):
        raise ValueError('Device ID must be 1..64 ASCII letters, digits, underscores or hyphens')
    key, key_info = public_key(key_file)
    def mac(interface):
        digest = hashlib.sha256(
            ('sunuefi:' + device_id + ':' + interface).encode()).digest()
        return ':'.join(f'{value:02x}' for value in bytes([0x02]) + digest[:5])
    return key, {'device_id': device_id, 'ssh_public_key': key_info,
                 'wifi_mac': mac('wifi'), 'usb_device_mac': mac('usb-device'),
                 'usb_host_mac': mac('usb-host'), 'usb_device_ipv4': '10.42.0.2/24',
                 'usb_host_ipv4': '10.42.0.1/24'}


def configure_debug_access(root, key, identity):
    """Apply personal access only after generic image identity sanitization."""
    put = stage_arch_piano.put
    put(root, 'etc/NetworkManager/conf.d/90-piano-debug.conf',
        '[device-piano-wifi]\nmatch-device=type:wifi\nwifi.scan-rand-mac-address=no\n'
        '\n[connection-piano-wifi]\nmatch-device=type:wifi\n'
        f'wifi.cloned-mac-address={identity["wifi_mac"]}\nipv4.dhcp-client-id=mac\n')
    put(root, 'etc/piano/usb-network.conf',
        f'USB_SERIAL={identity["device_id"]}\n'
        f'USB_DEVICE_MAC={identity["usb_device_mac"]}\n'
        f'USB_HOST_MAC={identity["usb_host_mac"]}\n')
    account = None
    for line in (root / 'etc/passwd').read_text().splitlines():
        name, _, uid, gid, _, home, _ = line.split(':')
        if name == 'piano':
            account = (int(uid), int(gid), home)
            break
    if not account or account[2] != '/home/piano':
        raise ValueError('The rootfs must contain the piano account and home')
    uid, gid, home = account
    directory = release.target(root, home.lstrip('/') + '/.ssh')
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    os.chown(directory, uid, gid)
    target = release.target(root, home.lstrip('/') + '/.ssh/authorized_keys')
    target.write_bytes(key)
    target.chmod(0o600)
    os.chown(target, uid, gid)
    root_key = release.target(root, 'root/.ssh/authorized_keys')
    if root_key.exists():
        root_key.unlink()
        root_key.parent.rmdir()
    # Verify the effective daemon policy without generating shared host keys.
    sshd_runtime = root / 'run/sshd'
    sshd_runtime.mkdir(parents=True, exist_ok=True)
    try:
        with bind_devices(root, lambda argv: run(*argv)), \
                tempfile.TemporaryDirectory(dir=root / 'run', prefix='sshd-check-') as folder:
            hostkey = Path(folder) / 'hostkey'
            run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', hostkey)
            effective = subprocess.check_output(
                ['chroot', root, '/usr/bin/sshd', '-T', '-h',
                 '/' + str(hostkey.relative_to(root))], text=True)
            settings = dict(line.lower().split(None, 1) for line in effective.splitlines()
                            if len(line.split(None, 1)) == 2)
            for name, accepted in (
                    ('pubkeyauthentication', {'yes'}),
                    ('passwordauthentication', {'no'}),
                    ('kbdinteractiveauthentication', {'no'}),
                    ('permitrootlogin', {'no'})):
                if settings.get(name) not in accepted:
                    raise ValueError('Unexpected SSH policy: ' + name)
    finally:
        sshd_runtime.rmdir()


def run(*args, **kwargs):
    return subprocess.run([str(value) for value in args], check=True, **kwargs)


def fetch_base(path, url):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix('.partial')
    with urllib.request.urlopen(url, timeout=90) as source, partial.open('wb') as target:
        shutil.copyfileobj(source, target)
    partial.replace(path)


def verify_base(archive, cfg, expected=None):
    """Require either an explicit digest or the official detached signature."""
    actual = release.digest(archive)
    if expected:
        if not re.fullmatch(r'[0-9a-f]{64}', expected) or actual != expected:
            raise ValueError('Arch Linux ARM bootstrap SHA256 differs from --base-sha256')
        return actual
    keyring = archive.parent / 'archlinuxarm-keyring.asc'
    signature = archive.with_suffix(archive.suffix + '.sig')
    fetch_base(keyring, cfg['keyring_url'])
    if release.digest(keyring) != cfg['keyring_sha256']:
        raise ValueError('Arch Linux ARM signing keyring SHA256 differs')
    fetch_base(signature, cfg['base_url'] + '.sig')
    with tempfile.TemporaryDirectory(prefix='arch-base-signature-') as folder:
        binary = Path(folder) / 'keyring.gpg'
        run('gpg', '--batch', '--no-options', '--homedir', folder,
            '--dearmor', '--output', binary, keyring)
        run('gpgv', '--homedir', folder, '--keyring', binary, signature, archive)
    return actual


def prepare_recipes(inputs, sensors):
    recipes = inputs / 'recipes'
    recipes.mkdir(parents=True, exist_ok=True)
    if not (recipes / 'fastrpc/PKGBUILD').is_file():
        sensor_recipes(recipes, sensors)
    mesa = recipes / 'mesa'
    if not (mesa / 'PKGBUILD').is_file():
        shutil.copytree(ROOT / 'linux/bsp/arch/mesa', mesa)
        shutil.copy2(
            ROOT / 'upstream/piano-mesa-current/patches/0001-freedreno-Add-chip-id-support-for-A830v1.patch',
            mesa)
    return recipes


def build(args):
    output = args.output.resolve()
    if os.geteuid() or not output.is_relative_to(ROOT / 'build/distros'):
        raise ValueError('Use root and an output below build/distros')
    if output.exists() and not args.resume:
        raise ValueError('Output already exists; use --resume for this build')
    if output.exists() and not (output / 'manifest.json').is_file() and not (
            args.resume and (output / 'FAILED.json').is_file() and (output / 'rootfs').is_dir()):
        raise ValueError('Existing output has no completed rootfs manifest; inspect it before resuming')
    if bool(args.device_id) != bool(args.ssh_public_key):
        raise ValueError('Provide --device-id and --ssh-public-key together')
    access = debug_identity(args.device_id, args.ssh_public_key) if args.device_id else None

    cfg = json.loads((ROOT / 'config/arch-niri.json').read_text())
    if args.base_url:
        cfg['base_url'] = assemble_rootfs.public_url(args.base_url)
    policy = json.loads((ROOT / 'config/release.json').read_text())
    archive = ROOT / 'build/arch-niri-inputs/ArchLinuxARM-aarch64.tar.gz'
    if not archive.is_file():
        fetch_base(archive, cfg['base_url'])
    base_hash = verify_base(archive, cfg, args.base_sha256)
    if output.exists():
        if (output / 'manifest.json').is_file():
            previous = json.loads((output / 'manifest.json').read_text())
            if (previous.get('distro'), previous.get('desktop')) != ('arch', 'niri'):
                raise ValueError('Resume requires an Arch+niri output')
            if previous.get('base_archive_sha256', previous.get('base_sha256')) != base_hash:
                raise ValueError('Resume requires the same verified bootstrap archive')
        else:
            failed = json.loads((output / 'FAILED.json').read_text())
            if failed.get('status') != 'BUILD_FAILED' or not (output / 'rootfs').is_dir():
                raise ValueError('Resume requires the failed build and its rootfs for inspection')

    root = output / 'rootfs'
    if not output.exists():
        options = assemble_rootfs.parser().parse_args([
            '--distro', 'arch', '--desktop', 'niri', '--execute',
            '--output', str(output), '--base-file', str(archive),
            '--base-url', cfg['base_url'], '--base-sha256', base_hash])
        assemble_rootfs.execute(assemble_rootfs.plan(options))
    elif not root.is_dir():
        raise ValueError('Existing output has no assembled rootfs')

    with release.build_resolver(root), guest_environment(root) as guest:
        guest(['pacman', '-S', '--needed', '--noconfirm', *cfg['hardware_packages']])
    inputs = ROOT / 'build/arch-niri-inputs'
    sensors = inputs / 'sensors'
    if not sensors.exists():
        sensor_sources(sensors, policy['sensors'])
    recipes = prepare_recipes(inputs, sensors)
    for name in ('mesa', 'fastrpc', 'libssc', 'iio-sensor-proxy'):
        packages = output / 'packages' / name
        if not (packages / 'manifest.json').is_file():
            build_arch_package.build(root, recipes / name, packages, resume=args.resume)

    ffmpeg = output / 'packages/ffmpeg'
    if not (ffmpeg / 'SOURCE.json').is_file():
        ffmpeg_resume = args.resume and (ffmpeg / 'source/SOURCE.json').is_file()
        if ffmpeg.exists() and not ffmpeg_resume and not any(ffmpeg.iterdir()):
            ffmpeg.rmdir()
        build_arch_ffmpeg.build(root, ffmpeg, resume=ffmpeg_resume)

    kernel = ROOT / 'artifacts/kernels/release'
    source_manifest = ROOT / 'build/release/source-manifest.json'
    kernel_build = ROOT / 'build/kernels/release'
    if not source_manifest.is_file() or not (kernel / 'manifest.json').is_file():
        raise ValueError('Build the Piano kernel first with ./build.sh linux')
    source = Path(json.loads(source_manifest.read_text())['worktree'])
    runtime = output / 'runtime'
    if not (runtime / 'manifest.json').is_file():
        if runtime.exists():
            if not args.resume:
                raise ValueError('Incomplete runtime build exists; inspect it and use --resume')
            shutil.rmtree(runtime)
        run(sys.executable, ROOT / 'tools/build_release_helpers.py',
            '--kernel', kernel, '--source', source,
            '--kernel-build', kernel_build, '--output', runtime,
            '--sysroot', args.bootstrap_sysroot.resolve(), '--cc', 'clang')

    stage_arch_piano.stage(argparse.Namespace(
        rootfs=root, kernel=kernel, runtime=runtime, sensor_source=sensors,
        bootstrap_sysroot=args.bootstrap_sysroot, desktop='niri'))
    record = json.loads((output / 'manifest.json').read_text())
    record.update(base_archive_sha256=base_hash, arch_niri_inputs=cfg,
                  ffmpeg=json.loads((ffmpeg / 'SOURCE.json').read_text()))
    (output / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    if access:
        configure_debug_access(root, *access)
        record['debug_access'] = access[1]
    (output / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    (output / 'FAILED.json').unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/distros/arch-niri-piano')
    parser.add_argument('--bootstrap-sysroot', type=Path, default=Path('/'))
    parser.add_argument('--base-sha256',
                        help='Expected bootstrap SHA256; otherwise verify its official detached signature')
    parser.add_argument('--base-url', help='Official HTTPS bootstrap mirror for this build')
    parser.add_argument('--device-id', help='Stable device serial used to derive fixed Wi-Fi and USB MACs')
    parser.add_argument('--ssh-public-key', type=Path,
                        help='Personal OpenSSH .pub key for piano; requires --device-id')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.worker:
        raise SystemExit(run('unshare', '--mount', '--propagation', 'private',
                             sys.executable, __file__, *sys.argv[1:], '--worker').returncode)
    build(args)
