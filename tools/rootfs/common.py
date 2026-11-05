"""Piano root policy, identity cleanup and initramfs assembly for Arch."""
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import time

import build_piano_disk_bootstrap as disk
import stage_piano_full_userspace as userspace
import stage_piano_kernel_modules as modules
from make_kernel_initramfs import make_newc, validate_static_arm64_elf

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / 'config/release.json'

def digest(path): return modules.sha(path)

def capture(args, cwd=None):
    return subprocess.check_output(list(map(str, args)), cwd=cwd, text=True).strip()

def own_root(rootfs, output):
    rootfs, output = Path(rootfs).resolve(), Path(output).resolve()
    if not output.is_relative_to(ROOT / 'build/distros') or rootfs != output / 'rootfs' or not rootfs.is_dir():
        raise ValueError('Modify only this new release build rootfs')
    return rootfs, output

def target(root, name):
    path = root / name
    if not path.is_relative_to(root) or '..' in Path(name).parts: raise ValueError('Guest path escapes root')
    for parent in path.parents:
        if parent == root: break
        if parent.is_symlink(): raise ValueError('Guest path traverses a symlink')
    return path

def put(root, name, text):
    path = target(root, name)
    if path.is_symlink(): path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)

def clock_epoch(rootfs):
    """Give systemd a build-time clock floor before RTC or NTP is available."""
    name = 'usr/lib/clock-epoch'
    put(rootfs, name, '')
    path = target(rootfs, name)
    path.chmod(0o644)
    timestamp = int(time.time())
    os.utime(path, (timestamp, timestamp))
    return {'path': '/' + name, 'mtime': timestamp,
            'utc': datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}

@contextmanager
def build_resolver(rootfs):
    # NetworkManager is not running inside the new build chroot yet.
    path = target(rootfs, 'etc/resolv.conf')
    link = path.readlink() if path.is_symlink() else None
    original = path.read_bytes() if path.is_file() and link is None else None
    mode = path.stat().st_mode & 0o777 if original is not None else 0o644
    path.unlink(missing_ok=True)
    path.write_bytes(Path('/etc/resolv.conf').read_bytes())
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        if link is not None: path.symlink_to(link)
        elif original is not None:
            path.write_bytes(original); path.chmod(mode)

def apply_policy(rootfs, output, config):
    rootfs, _ = own_root(rootfs, output)
    storage = config['storage']
    if (storage['root_label'], storage['root_partname'], storage['esp_label'], storage['root_fstype'], storage['esp_partname']) != ('PIANOROOT', 'sunuefi_root', 'SUNUEFI_ESP', 'ext4', 'sunuefi_esp'):
        raise ValueError('Unknown release storage identities')
    if set(storage['masked_units']) != {'piano-swapfile.service', 'qbootctl.service'}:
        raise ValueError('Only the two storage-policy units may be masked')
    put(rootfs, 'etc/fstab', 'LABEL=PIANOROOT / ext4 defaults,noatime,x-systemd.growfs 0 1\nLABEL=SUNUEFI_ESP /boot/efi vfat umask=0077,nofail 0 2\n')
    # The RAM overlay masks growfs; the dedicated disk root uses systemd's native service.
    growfs_mask = target(rootfs, 'etc/systemd/system/systemd-growfs-root.service')
    if growfs_mask.is_symlink() and growfs_mask.readlink() == Path('/dev/null'):
        growfs_mask.unlink()
    put(rootfs, 'etc/piano/root-policy.json', json.dumps({'version': 1, 'mode': 'label', 'root_label': 'PIANOROOT',
        'root_partlabel': 'sunuefi_root', 'esp_label': 'SUNUEFI_ESP'}, indent=2) + '\n')
    put(rootfs, 'etc/udev/rules.d/01-piano-protect-android.rules', '''SUBSYSTEM!="block", GOTO="release_end"
KERNELS!="1d84000.*", GOTO="release_end"
ENV{UDISKS_IGNORE}="1"
ENV{DEVTYPE}!="partition", GOTO="release_end"
IMPORT{builtin}="blkid"
ENV{ID_FS_LABEL}=="PIANOROOT", ENV{ID_PART_ENTRY_NAME}=="sunuefi_root", ENV{ID_FS_TYPE}=="ext4", GOTO="release_end"
ENV{ID_FS_LABEL}=="SUNUEFI_ESP", ENV{ID_PART_ENTRY_NAME}=="sunuefi_esp", ENV{ID_FS_TYPE}=="vfat", GOTO="release_end"
ATTR{ro}=="1", GOTO="release_end"
RUN+="/usr/sbin/blockdev --setro /dev/%k"
LABEL="release_end"
''')
    for unit in storage['masked_units']:
        path = target(rootfs, Path('etc/systemd/system') / unit)
        if path.exists() or path.is_symlink(): path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True); path.symlink_to('/dev/null')
    helper = target(rootfs, 'usr/lib/piano/piano-disk-hardware-prepare')
    if helper.exists() or helper.is_symlink(): helper.unlink()
    helper.symlink_to('piano-ram-hardware-prepare')
    debug = target(rootfs, Path('usr/local/sbin/piano-debug-bootstrap'))
    if debug.is_symlink(): debug.unlink()
    debug.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'linux/userspace/piano-debug-bootstrap', debug)
    put(rootfs, 'usr/local/sbin/piano-boot-request', (ROOT / 'linux/userspace/piano-boot-request').read_text())
    target(rootfs, 'usr/local/sbin/piano-boot-request').chmod(0o755)

def sanitize(rootfs, output):
    rootfs, output = own_root(rootfs, output)
    # These are populated by the running kernel after switch_root.
    for name in ('dev', 'proc', 'sys', 'run'):
        directory = target(rootfs, name)
        for child in directory.iterdir():
            if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
            else: child.unlink()
    for relative in ('root/.ssh/authorized_keys', 'home/piano/.ssh/authorized_keys'):
        path = target(rootfs, relative)
        if path.exists() or path.is_symlink(): path.unlink()
    for path in target(rootfs, 'etc/ssh/placeholder').parent.glob('ssh_host_*'):
        if path.is_file() or path.is_symlink(): path.unlink()
    for name in ('access-key', 'access-key.pub', 'login.txt'):
        path = output / name
        if path.exists() or path.is_symlink(): path.unlink()
    put(rootfs, 'etc/machine-id', '')
    path = target(rootfs, Path('var/lib/dbus/machine-id'))
    if path.exists() or path.is_symlink(): path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True); path.symlink_to('/etc/machine-id')
    login = json.loads(POLICY.read_text())['login']
    subprocess.run(['chroot', str(rootfs), '/usr/sbin/chpasswd'],
                   input=login['user'] + ':' + login['password'] + '\n',
                   text=True, check=True)
    for name in ('etc/shadow-', 'etc/gshadow-'):
        backup = target(rootfs, name)
        if backup.exists() or backup.is_symlink(): backup.unlink()

def native_boot_request(rootfs, output):
    # Compile inside the same ARM64 builder/root ABI used for the release.
    import build_boot_request
    folder = output / 'native-boot-request'
    compiler = os.environ.get('SUNUEFI_BOOT_REQUEST_CC') or (
        'aarch64-linux-gnu-gcc' if platform.machine() not in ('aarch64', 'arm64')
        and shutil.which('aarch64-linux-gnu-gcc') else 'clang')
    record = build_boot_request.build(folder, 'aarch64', compiler, rootfs)
    binary = target(rootfs, 'usr/local/sbin/piano-boot-request')
    shutil.copy2(folder / 'piano-boot-request', binary)
    binary.chmod(0o755)
    installed = {}
    for name, destination, mode in (
        ('piano-next-boot', 'usr/bin/piano-next-boot', 0o755),
        ('piano-next-boot-helper', 'usr/libexec/piano-next-boot-helper', 0o755),
        ('org.sunuefi.boot-request.policy', 'usr/share/polkit-1/actions/org.sunuefi.boot-request.policy', 0o644),
        ('49-piano-boot-request.rules', 'usr/share/polkit-1/rules.d/49-piano-boot-request.rules', 0o644),
        ('org.sunuefi.ReturnAndroid.desktop', 'usr/share/applications/org.sunuefi.ReturnAndroid.desktop', 0o644),
        ('org.sunuefi.BootMenu.desktop', 'usr/share/applications/org.sunuefi.BootMenu.desktop', 0o644),
    ):
        source = ROOT / 'linux/userspace' / name
        path = userspace.guest_resolve(rootfs, destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
        path.chmod(mode)
        installed[destination] = digest(source)
    record['next_boot_tools'] = installed
    # Remove only obsolete project-owned configuration from reused roots.
    for name in ('etc/piano/boot-request.json', 'usr/sbin/piano-next-boot-configure',
                 'usr/share/doc/piano-next-boot/boot-request.example.json'):
        userspace.guest_resolve(rootfs, name).unlink(missing_ok=True)
    record['boot_device_detection'] = 'read-only Qualcomm GPT A/B attributes'
    return record

def bootstrap(rootfs, kernel, output, release, boot_task_snapshot=False, firmware_root=None):
    # Use the same authenticated distro input on fresh CI and local builds.
    # The early diagnostic BusyBox capture is not a release dependency.
    busybox = disk.guest_resolve(rootfs, '/usr/bin/busybox')
    validate_static_arm64_elf(busybox.read_bytes())
    applets = set(capture(['chroot', rootfs, '/usr/bin/busybox', '--list']).splitlines())
    # Debian's static build omits mountpoint; use its util-linux executable
    # with the same ELF dependency closure already needed by blkid.
    bootstrap_applets = tuple(name for name in disk.APPLETS if name != 'mountpoint')
    if missing := set(bootstrap_applets) - applets:
        raise ValueError('Static BusyBox is missing bootstrap applets: ' + ', '.join(sorted(missing)))
    package = capture(['chroot', rootfs, 'dpkg-query', '-W',
                       '-f=${Package}\t${Version}\t${Architecture}', 'busybox-static']).split('\t')
    if len(package) != 3 or package[0] != 'busybox-static' or package[2] != 'arm64':
        raise ValueError('Release bootstrap requires the installed ARM64 busybox-static package')
    busybox_source = output / 'initramfs/busybox-source.json'
    busybox_source.parent.mkdir(exist_ok=True)
    busybox_record = {'package': package[0], 'version': package[1],
        'architecture': package[2], 'sha256': digest(busybox),
        'source': 'signed Debian APT in the release rootfs', 'license': 'GPL-2.0-only'}
    busybox_source.write_text(json.dumps(busybox_record, indent=2) + '\n')
    files = disk.runtime_files(rootfs, ('/usr/sbin/blkid', '/usr/bin/mountpoint'))
    directory = Path(kernel) / 'modules/lib/modules' / release
    ordered, _ = disk.module_closure(directory)
    rescue, _ = disk.module_closure(directory, disk.SEEDS + disk.RESCUE_SEEDS)
    files.update({'bin/busybox': busybox, 'pianoinit': ROOT / 'linux/userspace/disk-bootstrap',
                  'usr/local/sbin/piano-debug-bootstrap': ROOT / 'linux/userspace/piano-debug-bootstrap'})
    files['init'] = files['pianoinit']
    for name in rescue: files[f'lib/modules/{release}/{name}'] = directory / name
    files['lib/firmware/nanosic/MCU_Upgrade.bin'] = disk.guest_resolve(
        Path(firmware_root or rootfs), '/lib/firmware/nanosic/MCU_Upgrade.bin')
    generated = {'etc/piano/root-label': 'PIANOROOT\n', 'etc/piano/root-partname': 'sunuefi_root\n',
                 'etc/piano/busybox-source.json': busybox_source.read_text(),
                 'etc/piano/kernel-release': release + '\n',
                 'etc/piano/linux-debug.conf': 'usb=acm-ncm\nshell=1\nrecovery_seconds=0\n',
                 'etc/piano/modules-load-order': ''.join(f'{disk.module_name(p)} /lib/modules/{release}/{p}\n' for p in ordered),
                 'etc/piano/modules-rescue-order': ''.join(f'{disk.module_name(p)} /lib/modules/{release}/{p}\n' for p in rescue)}
    if boot_task_snapshot:
        generated['etc/piano/boot-task-snapshot'] = 'once\n'
    dirs = {'dev', 'proc', 'sys', 'run', 'tmp', 'sysroot'}
    for name in files.keys() | generated.keys(): dirs.update(p.as_posix() for p in Path(name).parents if p.as_posix() != '.')
    rows = [{'name': n, 'mode': stat.S_IFDIR | (0o1777 if n == 'tmp' else 0o755)} for n in sorted(dirs)]
    rows += [{'name': n, 'mode': stat.S_IFREG | 0o755, 'data': p.read_bytes()} for n, p in sorted(files.items())]
    rows += [{'name': n, 'mode': stat.S_IFREG | 0o644, 'data': v.encode()} for n, v in generated.items()]
    rows += [{'name': 'bin/' + n, 'mode': stat.S_IFLNK | 0o777, 'data': b'busybox'} for n in bootstrap_applets]
    rows += [{'name': 'dev/console', 'mode': stat.S_IFCHR | 0o600, 'major': 5, 'minor': 1},
             {'name': 'dev/null', 'mode': stat.S_IFCHR | 0o666, 'major': 1, 'minor': 3}]
    target = output / 'initramfs/initramfs.cpio.gz'; target.parent.mkdir(exist_ok=True)
    target.write_bytes(gzip.compress(make_newc(rows), mtime=0))
    record = {'status': 'HOST_BUILT_LABEL_BOOTSTRAP_NOT_BOOT_VERIFIED', 'kernel_release': release,
              'kernel_commit': json.loads((Path(kernel) / 'manifest.json').read_text())['source_commit'],
              'kernel_manifest_sha256': digest(Path(kernel) / 'manifest.json'), 'root_policy': 'LABEL=PIANOROOT',
              'initramfs_sha256': digest(target), 'entry_sha256': digest(files['pianoinit']),
              'busybox': busybox_record,
              'boot_task_snapshot': boot_task_snapshot,
              'boot_task_snapshot_source_sha256': digest(ROOT / 'linux/userspace/piano-boot-task-snapshot')}
    (target.parent / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    return record
