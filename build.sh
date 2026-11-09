#!/usr/bin/env bash
set -euo pipefail
sunuefi_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$sunuefi_root"
command="${1:-help}"
if (($#)); then shift; fi
case "$command" in
  sources) python3 tools/prepare_sources.py "$@" ;;
  check) python3 tools/check_host.py --group portable "$@" ;;
  uefi) make uefi ;;
  trampoline) python3 tools/package_product_trampoline.py "$@" ;;
  module) python3 tools/build_android_product_module.py "$@" ;;
  module-package) python3 tools/build_android_module.py "$@" ;;
  boot-request) python3 tools/build_boot_request.py "$@" ;;
  boot-repack) python3 tools/build_boot_repack.py "$@" ;;
  arch-niri-rootfs) python3 tools/build_arch_niri_rootfs.py "$@" ;;
  linux)
    python3 tools/prepare_release_kernel.py --refresh
    python3 - <<'PY_KERNEL'
import json, subprocess
from pathlib import Path
record=json.loads(Path('build/release/source-manifest.json').read_text())
subprocess.run(['python3','tools/build_piano_full_kernel.py','--worktree',record['worktree'],
 '--commit',record['actual_commit'],'--root','LABEL=PIANOROOT','--rebuild',
 '--build-dir','build/kernels/release','--artifacts','artifacts/kernels/release'],check=True)
PY_KERNEL
    ;;
  mesa)
    # Mesa is a native Debian package build; its Python dependencies come from
    # APT, not the isolated virtualenv used for UEFI font preparation.
    env -u VIRTUAL_ENV -u PYTHONHOME -u PYTHONPATH \
      PATH=/usr/sbin:/usr/bin:/sbin:/bin \
      bash upstream/piano-mesa-current/scripts/build-mesa-debs.sh \
      "${1:-$sunuefi_root/build/mesa}" 26.1.6-1~bpo13+1 ;;
  sensors)
    python3 - "$sunuefi_root" "${1:-$sunuefi_root/build/sensors}" "${@:2}" <<'PY_SENSORS'
import hashlib, json, os, shutil, subprocess, sys
from pathlib import Path
root, output = (Path(p).resolve() for p in sys.argv[1:3])
selection = sys.argv[3:]
if selection not in ([], ['--libssc-only']):
    raise SystemExit('Usage: ./build.sh sensors [OUTPUT_DIR] [--libssc-only]')
config = json.loads((root / 'config/release.json').read_text())['sensors']
if selection and 'patches/piano-sensors/0002-fix-libssc-property-types.patch' not in config['patches']:
    raise SystemExit('Select the libssc property-type patch in sensors.patches before building libssc-only.')
source = root / config['source']
head = subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], text=True).strip()
dirty = subprocess.check_output(['git', '-C', source, 'status', '--porcelain'], text=True)
if head != config['commit'] or dirty:
    raise SystemExit('Sensors source HEAD/cleanliness mismatch; run ./build.sh sources.')
workspace = root / 'build/sensors-workspace'
if workspace.exists():
    shutil.rmtree(workspace)
shutil.copytree(source, workspace, ignore=shutil.ignore_patterns('.git'))
patches = []
for name in config['patches']:
    patch = root / name
    patches.append({'path': name, 'sha256': hashlib.sha256(patch.read_bytes()).hexdigest()})
    subprocess.run(['patch', '--batch', '--forward', '-p1', '-i', patch], cwd=workspace, check=True)
record = {'source_url': config['source_url'], 'source_commit': head, 'patches': patches}
(workspace / 'SOURCE').write_text(json.dumps(record, indent=2) + '\n')
# Native Debian package tools must see their APT-installed Python modules.
# Keep the caller's virtualenv for UEFI and only isolate this child build.
build_env = os.environ.copy()
for name in ('VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH'):
    build_env.pop(name, None)
build_env['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
subprocess.run(['bash', workspace / 'scripts/build-sensors-debs.sh', output, *selection],
               env=build_env, check=True)
shutil.copy2(workspace / 'SOURCE', output / 'SOURCE')
PY_SENSORS
    ;;
  display-calibration) python3 tools/display/build_output_color.py "$@" ;;
  bsp)
    python3 tools/package_bsp.py "$@" ;;
  rootfs)
    python3 tools/assemble_rootfs.py "$@" ;;
  package|esp)
    if [[ "$command" == esp ]]; then set -- --esp-only "$@"; fi
    python3 - "$@" <<'PY_PACKAGE'
import json, subprocess, sys
from pathlib import Path
kernel = json.loads(Path('config/release.json').read_text())['kernel']
args = ['python3', 'tools/package_release.py',
        '--uefi', 'artifacts/product/PianoUEFI-product.img',
        '--kernel', 'artifacts/kernels/release',
        '--dtb', 'vendor/piano-linux/board.dtb',
        '--cpu-model-overlay', kernel['cpu_model_overlay'],
        '--panel-vendor', kernel['preferred_display_mode']['panel_vendor']]
for overlay in kernel['additional_dtb_overlays']:
    args += ['--dtb-overlay', overlay]
args += ['--initramfs', 'build/distros/arch-niri-piano/initramfs/initramfs.cpio.gz',
         '--rootfs', 'build/distros/arch-niri-piano/rootfs',
         '--rootfs-manifest', 'build/distros/arch-niri-piano/manifest.json',
         '--output', 'artifacts/release', '--root-selector', kernel['root_policy']]
subprocess.run(args + sys.argv[1:], check=True)
PY_PACKAGE
    ;;
  all)
    "$0" sources
    "$0" uefi
    "$0" module
    "$0" linux
    "$0" arch-niri-rootfs
    "$0" package "$@"
    ;;
  install) python3 tools/install_piano.py "$@" ;;
  installer) python3 tools/export_installer.py "$@" ;;
  help|-h|--help)
    printf '%s\n' 'Usage: ./build.sh sources|check|uefi|trampoline|module|module-package|boot-request|boot-repack|linux|mesa|sensors|display-calibration|bsp|rootfs|arch-niri-rootfs|package|all|installer|install' \
      'Full builds need the documented builder environment; rootfs and hardware packages run in a root ARM64 build container.' \
      'Building never partitions or flashes a tablet. install is a separate explicit command.' ;;
  *) printf 'Unknown command: %s\n' "$command" >&2; exit 2 ;;
esac
