#!/usr/bin/env python3
"""Build the native Goodix client with the standard Linux TEE object uAPI."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import shlex
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'linux/userspace/fingerprint'
CORE_SOURCES = (
    'piano-goodix-storage.c', 'piano-goodix-events.c',
    'piano-goodix-gatekeeper.c', 'piano-goodix-operations.c',
    'piano-goodix-rpmb.c', 'piano-goodix-tee.c', 'piano-goodix.c',
)
HEADERS = (
    'piano-goodix-storage.h', 'piano-goodix-events.h',
    'piano-goodix-gatekeeper.h', 'piano-goodix-operations.h',
    'piano-goodix-rpmb.h', 'piano-goodix-tee.h', 'piano-goodix-storage-protocol.h', 'piano-goodix.h',
)
CLI_SOURCE = NATIVE / 'piano-goodix-cli.c'
LICENSES = ('BSD-3-Clause.txt', 'BSD-3-Clause-Clear.txt')
ARCH_DESKTOP_DEPENDENCIES = {
    'libfprint': ('glib2', 'libgusb', 'libgudev', 'pixman', 'openssl'),
    'fprintd': ('libfprint', 'dbus', 'dbus-glib', 'polkit', 'pam'),
}
RUNTIME_PATHS = {
    'usr/local/sbin/piano-fingerprint-ctl',
    'usr/share/piano-provenance/fingerprint.json',
    'usr/share/licenses/piano-fingerprint/linux-tee.h',
    *{f'usr/share/licenses/piano-fingerprint/{name}.txt'
       for name in ('qcbor',)},
    *{f'usr/share/licenses/piano-fingerprint/{name}' for name in LICENSES},
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patches(record):
    if 'patch_series' not in record:
        return []
    series = ROOT / record['patch_series']
    return [(series.parent / entry['file'], entry['sha256'])
            for entry in json.loads(series.read_text())['patches']]


def source_inputs():
    config = json.loads((ROOT / 'config/fingerprint.json').read_text())
    patch_inputs = {}
    for record in (*config['sources'].values(), *config['desktop'].values()):
        if 'patch_series' in record:
            series = ROOT / record['patch_series']
            patch_inputs[record['patch_series']] = digest(series)
        for path, expected in patches(record):
            if digest(path) != expected:
                raise ValueError('Fingerprint patch checksum differs: ' + str(path))
            patch_inputs[str(path.relative_to(ROOT))] = expected
    return {
        'config_sha256': digest(ROOT / 'config/fingerprint.json'),
        'native_sources': {name: digest(NATIVE / name)
                           for name in (*CORE_SOURCES, *HEADERS, *LICENSES, CLI_SOURCE.name)},
        'builder_sha256': digest(Path(__file__)),
        'kernel_uapi_sha256': json.loads(
            (ROOT / 'config/release.json').read_text())['kernel']['uapi_sha256'],
        'firmware_manifest_sha256': digest(
            ROOT / 'vendor/firmware/hyperos4-fingerprint/manifest.json'),
        'patches': patch_inputs,
    }


def source(name, record):
    path = ROOT / 'build/source-cache' / name
    fresh = not path.exists()
    if fresh:
        subprocess.run(['git', 'clone', '--filter=blob:none', '--no-checkout',
                        record['url'], path], check=True)
    if not fresh and subprocess.check_output(
            ['git', '-C', path, 'status', '--porcelain', '--untracked-files=no'],
            text=True):
        raise ValueError('Fingerprint dependency has local changes: ' + name)
    if subprocess.run(['git', '-C', path, 'cat-file', '-e', record['commit']],
                      capture_output=True).returncode:
        subprocess.run(['git', '-C', path, 'fetch', '--depth=1', 'origin',
                        record['commit']], check=True)
    subprocess.run(['git', '-C', path, 'checkout', '--detach', record['commit']],
                   check=True)
    return path


def stage_sources(work, uapi):
    """Keep one source-built backend for the CLI and native libfprint builds."""
    work.mkdir(parents=True)
    cfg = json.loads((ROOT / 'config/fingerprint.json').read_text())['sources']
    dependencies = {}
    for name, record in cfg.items():
        upstream = source(name, record)
        destination, archive = work / name, work / (name + '.tar')
        destination.mkdir()
        subprocess.run(['git', '-C', upstream, 'archive', '--format=tar',
                        '--output=' + str(archive.resolve()), record['commit']], check=True)
        subprocess.run(['tar', '-xf', archive, '-C', destination], check=True)
        archive.unlink()
        for path, expected in patches(record):
            if digest(path) != expected:
                raise ValueError('Fingerprint patch checksum differs: ' + str(path))
            subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', path],
                           cwd=destination, check=True)
        dependencies[name] = destination
    for name in (*CORE_SOURCES, *HEADERS, *LICENSES):
        shutil.copy2(NATIVE / name, work / name)
    headers = work / 'uapi/linux'
    headers.mkdir(parents=True)
    shutil.copy2(uapi / 'include/linux/tee.h', headers / 'tee.h')
    return dependencies


def backend_archive(output, backend):
    archive = backend.parent / 'piano-goodix-sources.tar'
    subprocess.run(['tar', '--sort=name', '--mtime=@0', '--owner=0', '--group=0',
                    '--numeric-owner', '-cf', archive, '-C', backend, '.'], check=True)
    return {'file': str(archive.relative_to(output)), 'sha256': digest(archive)}


def prepare_desktop_backend(output, uapi):
    """Export the reviewed source used by libfprint without building the CLI."""
    inputs = source_inputs()
    backend = output / 'fingerprint/backend'
    stage_sources(backend, uapi)
    archive = backend_archive(output, backend)
    if source_inputs() != inputs:
        raise ValueError('Fingerprint inputs changed while preparing the backend')
    (backend.parent / 'fingerprint.json').write_text(json.dumps(
        {'inputs': inputs, 'backend_archive': archive}, indent=2) + '\n')


def build(output, flags, uapi, emulator=None):
    inputs = source_inputs()
    tee_header = uapi / 'include/linux/tee.h'
    tee_digest = digest(tee_header)
    cfg = json.loads((ROOT / 'config/fingerprint.json').read_text())
    work = output / 'fingerprint'
    work.mkdir()
    backend = work / 'backend'
    dependencies = stage_sources(backend, uapi)
    # Use the exported kernel interface before any sysroot system headers.
    flags = [flags[0], '-isystem', str(backend / 'uapi'), *flags[1:]]
    qcbor = dependencies['qcbor']
    includes = [qcbor / 'inc']
    originals = [
        *[qcbor / 'src' / name for name in (
            'UsefulBuf.c', 'qcbor_encode.c', 'qcbor_decode.c',
            'qcbor_err_to_str.c', 'ieee754.c')],
        *[backend / name for name in CORE_SOURCES],
        CLI_SOURCE,
    ]
    native_sources = {backend / name for name in CORE_SOURCES} | {CLI_SOURCE}
    objects = []
    for index, original in enumerate(originals):
        obj = work / (str(index) + '.o')
        command = [*flags, '-fPIC', '-D_GNU_SOURCE', '-DUSE_QCBOR',
                   *['-I' + str(p) for p in includes],
                   '-c', original, '-o', obj]
        # Imported Qualcomm sources retain their own warning policy.
        if original not in native_sources:
            command = [arg for arg in command if arg != '-Werror']
        subprocess.run(command, check=True)
        objects.append(obj)
    archive = backend_archive(output, backend)
    binary = work / 'piano-fingerprint-ctl'
    subprocess.run([*flags, '-static', *objects, '-pthread', '-lm', '-o', binary], check=True)
    subprocess.run(([emulator] if emulator else []) + [str(binary), '--help'], check=True)
    files = {}
    for name, path, mode in [
            ('usr/local/sbin/piano-fingerprint-ctl', binary, 0o755),
            ('usr/share/licenses/piano-fingerprint/linux-tee.h',
             backend / 'uapi/linux/tee.h', 0o644),
            *[(f'usr/share/licenses/piano-fingerprint/{name}', NATIVE / name, 0o644)
              for name in LICENSES],
            *[(f'usr/share/licenses/piano-fingerprint/{name}.txt',
               dependencies[name] / record['license'], 0o644)
              for name, record in cfg['sources'].items()]]:
        target = work / Path(name).name
        if path != target:
            shutil.copy2(path, target)
        files[name] = {'file': str(target.relative_to(output)),
                       'bytes': target.stat().st_size, 'sha256': digest(target), 'mode': mode}
    record = {
        'inputs': inputs,
        'sources': cfg['sources'],
        'client_source': str(CLI_SOURCE.relative_to(ROOT)),
        'client_sha256': digest(CLI_SOURCE),
        'source_files': {str(p.relative_to(ROOT)): digest(p) for p in originals},
        'backend_archive': archive,
        'uapi_headers': {'linux/tee.h': tee_digest},
        'firmware': json.loads(
            (ROOT / 'vendor/firmware/hyperos4-fingerprint/manifest.json').read_text())['files'],
        'operations': ['info', 'initialize', 'prepare-auth', 'watch', 'list',
                       'storage-probe', 'enroll', 'verify', 'delete'],
    }
    if source_inputs() != inputs:
        raise ValueError('Fingerprint sources changed during compilation')
    if digest(tee_header) != tee_digest or digest(backend / 'uapi/linux/tee.h') != tee_digest:
        raise ValueError('Fingerprint TEE header changed during compilation')
    provenance = work / 'fingerprint.json'
    provenance.write_text(json.dumps(record, indent=2) + '\n')
    files['usr/share/piano-provenance/fingerprint.json'] = {
        'file': str(provenance.relative_to(output)), 'bytes': provenance.stat().st_size,
        'sha256': digest(provenance), 'mode': 0o644}
    return files, record


def backend_sources(runtime):
    record = json.loads((runtime / 'fingerprint/fingerprint.json').read_text())
    if record.get('inputs') != source_inputs():
        raise ValueError('Rebuild the fingerprint runtime sources before packaging libfprint')
    archive = record.get('backend_archive', {})
    if archive.get('file') != 'fingerprint/piano-goodix-sources.tar':
        raise ValueError('Rebuild the fingerprint runtime; its backend source archive is missing')
    path = runtime / archive['file']
    if path.is_symlink() or not path.is_file() or digest(path) != archive.get('sha256'):
        raise ValueError('Fingerprint backend source archive differs from its build record')
    return path


def desktop_recipes(output, runtime):
    """Build the local libfprint backend and fprintd against the target distro ABI."""
    cfg = json.loads((ROOT / 'config/fingerprint.json').read_text())['desktop']
    backend = backend_sources(runtime)
    output.mkdir(parents=True, exist_ok=True)
    for name, record in cfg.items():
        recipe = output / name
        recipe.mkdir(exist_ok=True)
        upstream = source(name, record)
        archive = recipe / (name + '.tar')
        subprocess.run(['git', '-C', upstream, 'archive', '--format=tar',
                        '--prefix=' + name + '/', '--output=' + str(archive.resolve()),
                        record['commit']], check=True)
        patch_names = []
        for path, expected in patches(record):
            if digest(path) != expected:
                raise ValueError('Fingerprint desktop patch changed: ' + str(path))
            shutil.copy2(path, recipe / path.name)
            patch_names.append(path.name)
        local = ['piano-goodix-sources.tar'] if name == 'libfprint' else []
        for filename in local:
            shutil.copy2(backend, recipe / filename)
        files = [archive.name, *patch_names, *local]
        quoted = lambda values: ' '.join(shlex.quote(str(value)) for value in values)
        dependencies = ARCH_DESKTOP_DEPENDENCIES[name]
        prepare = ''.join('  patch -d "$srcdir/' + name + '" -p1 < "$srcdir/' + p + '"\n'
                          for p in patch_names)
        options = ('-Ddoc=false -Dgtk-examples=false -Dinstalled-tests=false '
                   '-Dudev_rules_dir=/usr/lib/udev/rules.d -Dudev_hwdb=disabled'
                   if name == 'libfprint' else '-Dgtk_doc=false -Dman=false')
        if name == 'libfprint':
            prepare += '''  mkdir -p "$srcdir/libfprint/libfprint/drivers/piano-goodix"
  tar -xf "$srcdir/piano-goodix-sources.tar" -C "$srcdir/libfprint/libfprint/drivers/piano-goodix"
'''
        notices = ''
        if name == 'libfprint':
            notices = '''  install -Dm644 "$srcdir/libfprint/libfprint/drivers/piano-goodix/BSD-3-Clause.txt" "$pkgdir/usr/share/licenses/libfprint/piano-goodix-BSD-3-Clause.txt"
  install -Dm644 "$srcdir/libfprint/libfprint/drivers/piano-goodix/BSD-3-Clause-Clear.txt" "$pkgdir/usr/share/licenses/libfprint/piano-goodix-BSD-3-Clause-Clear.txt"
  install -Dm644 "$srcdir/libfprint/libfprint/drivers/piano-goodix/qcbor/LICENSE" "$pkgdir/usr/share/licenses/libfprint/piano-goodix-QCBOR.txt"
  install -Dm644 "$srcdir/libfprint/libfprint/drivers/piano-goodix/uapi/linux/tee.h" "$pkgdir/usr/share/licenses/libfprint/piano-goodix-linux-tee.h"
'''
        recipe_text = (
            f'pkgname={name}\npkgver={record["version"]}\npkgrel=1.1\n'
            'arch=(aarch64)\nlicense=(LGPL-2.1-or-later)\n'
            f'pkgdesc="{name} with Piano fingerprint support"\n'
            f'url={record["url"]}\ndepends=({quoted(dependencies)})\n'
            'makedepends=(base-devel meson ninja glib2-devel gobject-introspection python-docutils perl)\n'
            'options=(!lto !debug)\n'
            + ('noextract=(piano-goodix-sources.tar)\n' if name == 'libfprint' else '') +
            f'source=({quoted(files)})\nsha256sums=({quoted(digest(recipe / f) for f in files)})\n'
            'prepare() {\n' + (prepare or '  :\n') + '}\n'
            'build() {\n' + f'  meson setup build {name} --prefix=/usr --libdir=lib '
            f'--buildtype=release {options}\n  meson compile -C build -j4\n}}\n'
            'package() {\n  DESTDIR="$pkgdir" meson install -C build\n' + notices + '}\n')
        (recipe / 'PKGBUILD').write_text(recipe_text)
    return cfg


def install_desktop(root, runtime, output):
    import build_arch_package
    from rootfs.arch import guest_environment
    from rootfs.common import build_resolver

    root, runtime, output = root.resolve(), runtime.resolve(), output.resolve()
    if (not root.is_relative_to(ROOT / 'build/distros') or
            not output.is_relative_to(ROOT / 'build') or output.is_relative_to(root)):
        raise ValueError('Build fingerprint packages outside a project-built target rootfs')
    recipes = output / 'recipes'
    desktop_recipes(recipes, runtime)
    for name in ('libfprint', 'fprintd'):
        required = ('glibc', *ARCH_DESKTOP_DEPENDENCIES[name])
        missing = subprocess.run(['chroot', root, 'pacman', '-T', *required],
                                 capture_output=True, text=True)
        if missing.returncode not in (0, 127):
            raise RuntimeError(missing.stderr)
        if missing.returncode:
            with build_resolver(root), guest_environment(root) as guest:
                guest(['pacman', '-S', '--needed', '--noconfirm', *missing.stdout.splitlines()])
        packages = output / name
        cohort = subprocess.check_output(
            ['chroot', root, 'pacman', '-Q', *required], text=True)
        inputs = {'recipe_sha256': build_arch_package.recipe_sha256(recipes / name / 'PKGBUILD'),
                  'runtime_packages': cohort}
        if not (packages / 'manifest.json').is_file():
            build_arch_package.build(root, recipes / name, packages)
            updated = subprocess.check_output(
                ['chroot', root, 'pacman', '-Q', *required], text=True)
            if updated != cohort:
                raise ValueError('Fingerprint build dependencies changed the target ABI; rebuild the packages')
            (packages / 'fingerprint-inputs.json').write_text(json.dumps(inputs, indent=2) + '\n')
        else:
            marker = packages / 'fingerprint-inputs.json'
            if not marker.is_file() or json.loads(marker.read_text()) != inputs:
                raise ValueError('Fingerprint package sources or target ABI changed; use a fresh output')
            build_arch_package.install_cached(root, packages)
    desktop_service(root)


def desktop_service(root):
    config = root / 'usr/lib/systemd/system/fprintd.service.d/20-piano.conf'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[Service]\nStateDirectory=piano-goodix\nStateDirectoryMode=0700\n'
                      'DeviceAllow=char-uio rw\nDeviceAllow=char-tee rw\nDeviceAllow=char-bsg rw\n')
