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
    python3 tools/build/kernel.py "$@"
    ;;
  mesa) python3 tools/build/mesa.py "$@" ;;
  sensors) python3 tools/build/sensors.py "$@" ;;
  display-calibration) python3 tools/display/build_output_color.py "$@" ;;
  bsp)
    python3 tools/package_bsp.py "$@" ;;
  rootfs)
    python3 tools/assemble_rootfs.py "$@" ;;
  package|esp)
    if [[ "$command" == esp ]]; then set -- --esp-only "$@"; fi
    python3 tools/package/release.py "$@"
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
