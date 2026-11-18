"""Stage the real BSP payload into guest roots without following guest links."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import stage_piano_ram_hardware as stage


class HardwareStagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        shutil.copytree(ROOT / 'linux/bsp/common',
                        self.workspace / 'linux/bsp/common', symlinks=True)
        for name in ('linux/userspace/piano-ram-hardware-prepare',
                     'tools/check_piano_kernel_dma_routes.py',
                     'tools/check_piano_kernel_contexts.py'):
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        self.pin = 'a' * 40
        (self.workspace / 'sources.lock.json').write_text(json.dumps(
            {'debian-piano-current': {'commit': self.pin}}))
        self.guest = self.workspace / 'build/distros/arch/rootfs'
        self.guest.mkdir(parents=True)

    def build(self, name):
        # Only pinned-source Git metadata is substituted; all staging is real.
        with patch.object(stage, 'ROOT', self.workspace), \
                patch.object(stage.subprocess, 'check_output',
                             side_effect=[self.pin + '\n', '']):
            return stage.build(self.workspace / 'build' / name,
                               self.workspace / 'public-source', self.guest)

    def test_parent_symlink_is_rejected_before_output_is_created(self):
        outside = self.workspace / 'host-directory'
        outside.mkdir()
        (self.guest / 'usr').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'guest symlink'):
            self.build('rejected')
        self.assertFalse((self.workspace / 'build/rejected').exists())
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
