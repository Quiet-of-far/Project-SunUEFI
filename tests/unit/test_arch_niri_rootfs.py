"""Bootstrap authentication and portable Arch builder dependencies; no device."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import build_arch_niri_rootfs as builder
from rootfs import arch
import build_piano_runtime_helpers as runtime


class ArchNiriTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name) / 'base.tar.gz'
        self.archive.write_bytes(b'fixture bootstrap')
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_explicit_digest_is_checked_before_guest_execution(self):
        with patch.object(builder, 'fetch_base', side_effect=AssertionError('download')):
            self.assertEqual(builder.verify_base(self.archive, {}, self.digest), self.digest)
            with self.assertRaisesRegex(ValueError, 'SHA256 differs'):
                builder.verify_base(self.archive, {}, '0' * 64)
            with self.assertRaisesRegex(ValueError, 'SHA256 differs'):
                builder.verify_base(self.archive, {}, '../invalid')

    def test_signature_failure_propagates_and_keyring_is_pinned(self):
        keyring = b'fixture public signing keys'
        cfg = {'base_url': 'https://example.invalid/base.tar.gz',
               'keyring_url': 'https://example.invalid/public.asc',
               'keyring_sha256': hashlib.sha256(keyring).hexdigest()}

        def fetch(path, url):
            path.write_bytes(keyring if url == cfg['keyring_url'] else b'fixture signature')

        def execute(*args):
            if args[0] == 'gpgv':
                raise subprocess.CalledProcessError(1, args)

        with patch.object(builder, 'fetch_base', side_effect=fetch), \
                patch.object(builder, 'run', side_effect=execute) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                builder.verify_base(self.archive, cfg)
            self.assertEqual(run.call_count, 2)
            self.assertIn('--keyring', run.call_args.args)
            cfg['keyring_sha256'] = '0' * 64
            run.reset_mock()
            with self.assertRaisesRegex(ValueError, 'keyring SHA256 differs'):
                builder.verify_base(self.archive, cfg)
            run.assert_not_called()

    def test_sensor_sources_use_the_fixed_clean_checkout_and_record_patches(self):
        cfg = {'source': 'upstream/sensor-fixture', 'commit': 'a' * 40,
               'source_url': 'https://example.invalid/sensors.git', 'patches': ['fix.patch']}
        with patch.object(arch.subprocess, 'check_output',
                          side_effect=[cfg['commit'] + '\n', '']), \
                patch.object(arch.shutil, 'copytree'), \
                patch.object(arch, 'digest', return_value='b' * 64), \
                patch.object(arch, 'run') as run:
            arch.sensor_sources(Path(self.temp.name), cfg)
        self.assertEqual(run.call_args.args[0][:5],
                         ['patch', '--batch', '--forward', '-p1', '-i'])
        record = json.loads((Path(self.temp.name) / 'SOURCE').read_text())
        self.assertEqual(record['source_commit'], cfg['commit'])
        self.assertEqual(record['patches'][0]['sha256'], 'b' * 64)

    def test_removed_products_are_rejected_by_build_entry(self):
        for name in ('release-rootfs', 'steamos-rootfs', 'nyarch-rootfs'):
            result = subprocess.run(['bash', ROOT / 'build.sh', name],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Unknown command', result.stderr)

    def test_arch_runtime_installs_camera_and_matching_module_with_exact_paths(self):
        workspace = Path(self.temp.name)
        root = workspace / 'build/distros/arch/rootfs'
        bundle = workspace / 'runtime'
        release = '7.3.3-piano-test'
        (root / 'usr/lib/modules' / release / 'kernel').mkdir(parents=True)
        bundle.mkdir()
        blob = bundle / 'artifact'
        blob.write_bytes(b'compiled payload')
        module = f'usr/lib/modules/{release}/updates/v4l2loopback.ko'
        camera = {
            'usr/lib/piano/piano-camerad',
            'usr/bin/piano-camera-ctl',
            'usr/share/licenses/piano-camera/LICENSE-MIT',
            'usr/share/licenses/piano-quad/LICENSE-MIT',
            'usr/share/licenses/demosaic/LICENSE-MIT',
            module,
        }
        basic = {row[2] for row in runtime.PUBLIC_SOURCES.values()} | runtime.PEN_RUNTIME_PATHS
        manifest = {
            'kernel': {'release': release}, 'pen_core': {'input': {}},
            'runtime_files': {
                name: {'file': blob.name, 'mode': 0o644,
                       'bytes': blob.stat().st_size, 'sha256': runtime.sha(blob)}
                for name in (basic | camera) - {module}
            },
        }
        manifest_path = bundle / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest))
        with patch.object(runtime, 'ROOT', workspace), \
                patch.object(runtime, 'pen_kernel_inputs', return_value=({}, {})):
            with self.assertRaisesRegex(ValueError, 'exact contract'):
                runtime.stage(bundle, root, include_camera=True)
            self.assertFalse((root / 'usr/lib/piano/piano-camerad').exists())
            manifest['runtime_files'][module] = manifest['runtime_files']['usr/lib/piano/piano-camerad']
            manifest_path.write_text(json.dumps(manifest))
            runtime.stage(bundle, root, include_camera=True)
        self.assertTrue(all((root / name).read_bytes() == blob.read_bytes() for name in camera))

    def test_mesa_upgrade_protection_preserves_other_pacman_configuration(self):
        root = Path(self.temp.name)
        (root / 'etc').mkdir()
        path = root / 'etc/pacman.conf'
        original = ('[options]\nIgnorePkg = custom-kernel mesa\n'
                    'SigLevel = Required DatabaseOptional\n'
                    '[core]\nInclude = /etc/pacman.d/mirrorlist\n')
        path.write_text(original)
        arch.protect_mesa_packages(root)
        protected = path.read_text()
        self.assertIn('IgnorePkg = custom-kernel mesa\n', protected)
        self.assertIn('IgnorePkg = vulkan-freedreno\n', protected)
        self.assertIn('[core]\nInclude = /etc/pacman.d/mirrorlist\n', protected)
        self.assertIn('SigLevel = Required DatabaseOptional\n', protected)
        arch.protect_mesa_packages(root)
        self.assertEqual(path.read_text(), protected)


if __name__ == '__main__':
    unittest.main()
