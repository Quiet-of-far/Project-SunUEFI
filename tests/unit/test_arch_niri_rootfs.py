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


if __name__ == '__main__':
    unittest.main()
