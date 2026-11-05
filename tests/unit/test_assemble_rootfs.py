"""Host fixtures for distro/desktop dispatch; no base download or package install."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
SPEC = importlib.util.spec_from_file_location('assemble_fixture', ROOT / 'tools/assemble_rootfs.py')
tool = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(tool)


class AssembleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / 'linux/rootfs', self.root / 'linux/rootfs')
        shutil.copytree(ROOT / 'linux/desktops', self.root / 'linux/desktops')

    def args(self, distro='arch', desktop='niri', *extra):
        return tool.parser().parse_args(['--distro', distro, '--desktop', desktop, '--plan', *extra])

    def package(self, name, target, component='config', ready=True, extension='.pkg.tar.zst'):
        folder = self.root / name; folder.mkdir()
        filename = name + extension; data = ('fixture ' + name).encode(); (folder / filename).write_bytes(data)
        manifest = {'schema_version': 1, 'component': component, 'package_ready': ready, 'target': target,
                    'files': {filename: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'format': 'deb'}}}
        (folder / 'manifest.json').write_text(json.dumps(manifest)); return folder

    def test_plan_is_readonly_and_reports_recipe_not_real_boot(self):
        with patch.object(tool.subprocess, 'run', side_effect=AssertionError('plan executed command')):
            result = tool.plan(self.args('arch', 'niri', '--base-sha256', 'a' * 64), self.root)
        self.assertEqual(result['status'], 'HOST_RECIPE_CANDIDATE'); self.assertFalse(result['device_tested'])
        self.assertEqual(result['storage']['root_partname'], 'sunuefi_root')
        self.assertEqual(result['storage']['root_label'], 'PIANOROOT')
        self.assertFalse(Path(result['output']).exists())


    def test_arch_requires_real_tarball_digest_and_has_no_fallback(self):
        result = tool.plan(self.args('arch', 'niri'), self.root)
        self.assertEqual(result['status'], 'INPUTS_REQUIRED'); self.assertIn('base_sha256', result['missing_inputs'])
        with patch.object(tool.subprocess, 'run', side_effect=AssertionError('missing hash downloaded')):
            with self.assertRaisesRegex(ValueError, 'Required explicit'):
                tool.execute(result)
        self.assertFalse(Path(result['output']).exists())

    def test_arch_niri_plan_selects_niri_packages_without_claiming_device_support(self):
        result = tool.plan(self.args('arch', 'niri'), self.root)
        self.assertEqual(result['status'], 'INPUTS_REQUIRED')
        self.assertEqual(result['desktop_profile']['display_manager']['arch'], 'greetd')
        self.assertIn('niri', result['desktop_profile']['arch_packages'])
        self.assertIn('Squeekboard', result['desktop_profile']['osk'])
        self.assertIn('unverified', result['desktop_profile']['osk'])
        self.assertIn('unverified', result['desktop_profile']['rotation'])

    def test_niri_session_configuration_uses_greetd_without_forced_scaling(self):
        root = self.root / 'build/distros/arch-niri/rootfs'
        root.mkdir(parents=True)
        result = {'desktop': 'niri', 'scale_percent': 200,
                  'desktop_profile': {'display_manager': {'arch': 'greetd'}}, 'distro': 'arch'}
        guest = lambda *args, **kwargs: None
        tool.configure_desktop(root, result, guest, [])
        config = (root / 'etc/skel/.config/niri/config.kdl').read_text()
        self.assertNotIn('scale', config)
        greetd = (root / 'etc/greetd/config.toml').read_text()
        self.assertIn('[initial_session]', greetd)
        self.assertIn('command = "niri-session"', greetd)
        self.assertIn('tuigreet', greetd)
        self.assertIn('disable-power-key-handling', config)
        self.assertEqual((root / 'etc/systemd/logind.conf.d/50-niri-power.conf').read_text(),
                         '[Login]\nHandlePowerKey=suspend\n')
        self.assertIn('polkit-gnome-authentication-agent-1', config)
        self.assertIn('XF86AudioRaiseVolume', config)



    def test_output_overwrite_or_escape_is_rejected(self):
        output = self.root / 'build/distros/old'; output.mkdir(parents=True); (output / 'keep').write_text('old')
        for path in (output, self.root / 'outside'):
            with self.assertRaisesRegex(ValueError, 'new output'):
                tool.plan(self.args('arch', 'niri', '--output', str(path)), self.root)
        self.assertEqual((output / 'keep').read_text(), 'old')

    def test_package_target_and_bytes_are_checked(self):
        target = {'distro': 'arch', 'suite': 'rolling', 'arch': 'aarch64'}
        directory = self.package('config', target)
        self.assertEqual(tool.bundle(directory, target)['component'], 'config')
        with self.assertRaisesRegex(ValueError, 'target distro'):
            tool.bundle(directory, {**target, 'distro': 'ubuntu', 'suite': 'noble'})
        (directory / 'config.pkg.tar.zst').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'identity changed'): tool.bundle(directory, target)

    def test_arch_rejects_debian_package_even_with_forged_target_label(self):
        target = {'distro': 'arch', 'suite': 'rolling', 'arch': 'aarch64'}
        directory = self.package('config', target, extension='.deb')
        with self.assertRaisesRegex(ValueError, 'Package format'): tool.bundle(directory, target)

    def test_recipe_only_bsp_and_missing_compiled_layers_are_not_complete(self):
        target = {'distro': 'arch', 'suite': 'rolling', 'arch': 'aarch64'}
        recipe = self.package('recipe', target, ready=False)
        with self.assertRaisesRegex(ValueError, 'not a built'): tool.bundle(recipe, target)
        bsp = self.package('config', target)
        result = tool.plan(self.args('arch', 'niri', '--bsp', str(bsp)), self.root)
        self.assertEqual(set(result['missing_inputs']), {'base_sha256', 'mesa', 'runtime', 'modules', 'firmware'})
        self.assertEqual(result['status'], 'INPUTS_REQUIRED')

    def test_complete_target_layers_use_bsp_manifest_fields(self):
        target = {'distro': 'arch', 'suite': 'rolling', 'arch': 'aarch64'}
        bsp = self.package('config', target); extra = ['--bsp', str(bsp), '--base-sha256', 'a' * 64]
        for component in sorted(tool.LAYERS): extra += ['--layer', str(self.package(component, target, component))]
        result = tool.plan(self.args('arch', 'niri', *extra), self.root)
        self.assertEqual(result['status'], 'HOST_RECIPE_CANDIDATE')
        self.assertEqual({row['component'] for row in result['layers']}, tool.LAYERS)



    def test_tar_parent_symlink_escape_is_rejected_before_extract(self):
        archive = self.root / 'bad.tar'
        with tarfile.open(archive, 'w') as tar:
            link = tarfile.TarInfo('etc'); link.type = tarfile.SYMTYPE; link.linkname = '/old-root'; tar.addfile(link)
            child = tarfile.TarInfo('etc/fstab'); child.size = 1; tar.addfile(child, io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError, 'symlink-parent'): tool.validate_archive(archive)



if __name__ == '__main__': unittest.main()
