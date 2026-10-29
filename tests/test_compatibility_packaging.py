"""Packaging must not turn evidence for a different binary into activation."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

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

    def test_staging_copy_is_rechecked_before_executing_policy_or_publishing(self):
        installer = load('install-greeter-fix')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = object.__new__(installer.Installer)
            base.root = root
            base.owner_uid, base.owner_gid = os.getuid(), os.getgid()
            base.package = root / 'package'
            policy = base.package / 'payload/takeover/compatibility.py'
            policy.parent.mkdir(parents=True)
            policy.write_text('original policy')
            manifest = {'files': {'takeover/compatibility.py': installer.digest(policy)}, 'symlinks': {}}
            copy = shutil.copytree
            def corrupt_copy(source, destination, *args, **kwargs):
                result = copy(source, destination, *args, **kwargs)
                if Path(source) == base.package / 'payload':
                    (destination / 'takeover/compatibility.py').write_text('modified during copy')
                return result
            with patch.object(installer.shutil, 'copytree', side_effect=corrupt_copy), \
                    patch.object(base, 'verify_candidate_compatibility') as inspect:
                with self.assertRaisesRegex(RuntimeError, 'candidate changed'):
                    base.stage_prefix(manifest)
                inspect.assert_not_called()
            self.assertFalse(base.path(installer.PREFIX).exists())

    def test_finalization_only_seals_validated_receipts_without_changing_payload(self):
        finalizer = load('finalize-compatibility-bundle')
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            payload = bundle / 'payload'
            payload.mkdir()
            (payload / 'binary').write_text('tested binary')
            anchor = bundle / 'payload-manifest.json'
            anchor.write_text(json.dumps(finalizer.inventory(payload)))
            original = anchor.read_bytes()
            (payload / 'build').mkdir()
            (payload / finalizer.MANIFEST).write_text(json.dumps({'runtime_prefix': '/opt/final'}))
            (payload / finalizer.VALIDATION).write_text('{}')
            with patch.object(finalizer, 'inspect_candidate', return_value={'status': 'needs-validation'}):
                with self.assertRaisesRegex(RuntimeError, 'has not passed'):
                    finalizer.finalize(bundle)
            self.assertEqual(anchor.read_bytes(), original)
            (payload / 'binary').write_text('changed after testing')
            with patch.object(finalizer, 'inspect_candidate') as inspect:
                with self.assertRaisesRegex(RuntimeError, 'packaged files changed'):
                    finalizer.finalize(bundle)
                inspect.assert_not_called()
            (payload / 'binary').write_text('tested binary')
            with patch.object(finalizer, 'inspect_candidate', return_value={'status': 'verified', 'profile_id': 'fixture'}):
                report = finalizer.finalize(bundle)
            self.assertFalse(report['deployment_authorized'])
            self.assertEqual(json.loads(anchor.read_text()), finalizer.inventory(payload))


if __name__ == '__main__':
    unittest.main()
