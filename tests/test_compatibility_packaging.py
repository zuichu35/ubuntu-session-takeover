"""Packaging must not turn evidence for a different binary into activation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompatibilityPackaging(unittest.TestCase):
    def test_rebuilt_payload_drops_old_receipts_and_keeps_policy(self):
        builder = load('package-compatibility')
        with tempfile.TemporaryDirectory() as directory:
            source, payload = Path(directory) / 'source', Path(directory) / 'payload'
            for relative in (*builder.FILES, 'compat/profiles/target.json'):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            for relative in builder.RECEIPTS:
                path = payload / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('previous binary evidence')
            builder.stage(source, payload)
            self.assertTrue((payload / 'compat/profiles/target.json').is_file())
            for relative in builder.RECEIPTS:
                self.assertFalse((payload / relative).exists())
            with self.assertRaises(RuntimeError):
                builder.stage(source, source)

    def test_staging_cannot_write_through_payload_symlink(self):
        builder = load('package-compatibility')
        with tempfile.TemporaryDirectory() as directory:
            payload, outside = Path(directory) / 'payload', Path(directory) / 'outside'
            payload.mkdir()
            outside.mkdir()
            (payload / 'takeover').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'escapes'):
                builder.stage(ROOT, payload)
            self.assertEqual(list(outside.iterdir()), [])

    def test_typelib_copy_uses_profile_and_rejects_wrong_library(self):
        types = load('native-typelibs')
        with tempfile.TemporaryDirectory() as directory:
            root, system = Path(directory) / 'build', Path(directory) / 'system'
            # Deliberately noncurrent ABI: the mapping must not be derived from
            # GNOME's major version or a hard-coded ABI 18 destination.
            source = system / 'usr/lib/example/mutter-77/Meta-77.typelib'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'metadata\0libmutter-77.so.0\0')
            relative = 'build/native-prefix/lib/example/mutter-77/Meta-77.typelib'
            profile = {'paths': {'library_subdir': 'lib/example', 'mutter_subdir': 'mutter-77'},
                'system_files': ['/usr/lib/example/mutter-77/Meta-77.typelib'],
                'artifacts': [{'kind': 'typelib', 'path': relative, 'library': 'libmutter-77.so.0'}]}
            types.copy_typelibs(root, profile, system)
            self.assertEqual((root / relative).read_bytes(), source.read_bytes())
            source.write_bytes(b'libmutter-18.so.0\0')
            with self.assertRaisesRegex(RuntimeError, 'metadata'):
                types.copy_typelibs(root, profile, system)

    def test_installer_rejects_unvalidated_staged_policy_before_activation(self):
        installer = load('install-greeter-fix')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'takeover/compatibility.py'
            source.parent.mkdir()
            base = object.__new__(installer.Installer)
            base.root = Path('/')
            for status, eligible in (('needs-validation', True), ('incompatible', True), ('verified', False)):
                source.write_text('def inspect_candidate(root, **kwargs):\n'
                    '    assert kwargs["required_owner"] == 0\n'
                    '    return ' + repr({'status': status, 'production_eligible': eligible}) + '\n')
                with self.assertRaisesRegex(RuntimeError, 'not verified'):
                    base.verify_candidate_compatibility(root)
            # No import cache or install/configuration outputs may be created.
            self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()),
                             ['takeover/compatibility.py'])


if __name__ == '__main__':
    unittest.main()
