"""Pure compatibility fixtures plus read-only path/ELF parsers; no actual desktop."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch

from takeover import compatibility as compat

ROOT = Path(__file__).resolve().parents[1]


def profile_fixture():
    raw = (ROOT / 'compat/profiles' / (compat.DEFAULT_PROFILE + '.json')).read_bytes()
    return json.loads(raw), compat.sha256(raw)


def verified_facts(profile, digest):
    artifacts = {item['path']: hashlib.sha256(item['path'].encode()).hexdigest() for item in profile['artifacts']}
    system = {name: hashlib.sha256(name.encode()).hexdigest() for name in profile['system_files']}
    facts = {'paths': compat.runtime_paths('/opt/fixture', profile), 'production_eligible': True,
             'profile_sha256': digest, 'target': copy.deepcopy(profile['target']),
             'artifacts': artifacts, 'private_inventory': {'files': dict(artifacts), 'symlinks': {}},
             'system_hashes': system, 'errors': [], 'diagnostics': [],
             'shell_elf': {'machine': 62, 'needed': profile['shell_needed']}, 'elf': {}, 'typelibs': {}}
    facts['private_inventory']['files'].update({name: hashlib.sha256(name.encode()).hexdigest()
                                               for name in profile['runtime_code']})
    facts['shell_linkage'] = {name: {'machine': 62, 'needed': list(profile['shell_needed']),
                                    'soname': None, 'runpaths': []}
                              for name in profile['shell_linkage_files']}
    for item in profile['artifacts']:
        if item['kind'] == 'elf':
            facts['elf'][item['path']] = {'machine': 62, 'soname': item.get('soname'), 'exports': item.get('exports', [])}
        elif item['kind'] == 'typelib':
            facts['typelibs'][item['path']] = True
    manifest = compat.build_manifest(profile, facts, runtime_prefix='/opt/fixture', sources=profile['sources'])
    facts['manifest'] = manifest
    facts['manifest_sha256'] = hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
    facts['receipt'] = {'schema': 1, 'scope': 'target-shell-integration', 'profile_id': profile['id'],
                        'manifest_sha256': facts['manifest_sha256'], 'target': copy.deepcopy(profile['target']),
                        'cases': {name: 'passed' for name in profile['required_cases']},
                        'tested_binaries': {**{'runtime:' + name: artifacts[name] for name in profile['tested_runtime']},
                                            **{'system:' + name: system[name] for name in profile['tested_system']}}}
    # Avoid shared mutable dictionaries between observed facts and recorded evidence.
    return copy.deepcopy(facts)


class Decisions(unittest.TestCase):
    def setUp(self):
        self.profile, digest = profile_fixture()
        self.facts = verified_facts(self.profile, digest)

    def report(self):
        return compat.evaluate(self.profile, self.facts)

    def test_exact_target_receipt_is_required_and_environment_is_self_contained(self):
        report = self.report()
        self.assertEqual(report['status'], 'verified')
        with patch.dict(os.environ, LD_LIBRARY_PATH='/evil', GI_TYPELIB_PATH='/evil'):
            env = compat.private_environment(report)
        self.assertNotIn('/evil', ':'.join(env.values()))
        self.facts['receipt'] = None
        report = self.report()
        self.assertEqual(report['status'], 'needs-validation')
        self.assertTrue(report['structural_ok'])
        with self.assertRaises(compat.CompatibilityError):
            compat.private_environment(report)

    def test_cross_abi_is_incompatible_even_if_versions_claim_match(self):
        self.facts['target']['mutter_abi'] = '19'
        self.facts['shell_elf']['needed'] = ['libmutter-19.so.0']
        self.assertEqual(self.report()['status'], 'incompatible')

    def test_same_abi_package_patch_drift_requires_validation(self):
        self.facts['target']['packages']['gnome-shell'] = '50.1-0ubuntu1.99'
        report = self.report()
        self.assertEqual(report['status'], 'needs-validation')
        self.assertTrue(report['structural_ok'])

    def test_same_hashes_do_not_authorize_any_50_x_version(self):
        self.facts['target']['packages']['gnome-shell'] = '50.9-0ubuntu1'
        self.assertEqual(self.report()['status'], 'needs-validation')

    def test_host_hash_drift_is_diagnostic_only(self):
        self.facts['system_hashes']['/usr/bin/gnome-shell'] = 'c' * 64
        report = self.report()
        self.assertEqual(report['status'], 'verified')
        self.assertTrue(any('fingerprint changed' in message for message in report['diagnostics']))

    def test_shell_new_dependency_or_libshell_runpath_drift_needs_validation(self):
        for absolute, field, value in (('/usr/bin/gnome-shell', 'needed', 'libmissing-new.so.0'),
                                       ('/usr/lib/gnome-shell/libshell-18.so', 'runpaths', '/new/library/path')):
            with self.subTest(path=absolute):
                facts = copy.deepcopy(self.facts)
                facts['shell_linkage'][absolute][field].append(value)
                facts['system_hashes'][absolute] = 'a' * 64
                self.assertEqual(compat.evaluate(self.profile, facts)['status'], 'needs-validation')

    def test_missing_or_corrupt_build_manifest_is_incompatible(self):
        for value in (None, [], {'schema': 1}, {'schema': 2}):
            with self.subTest(value=value):
                self.facts['manifest'] = value
                self.assertEqual(self.report()['status'], 'incompatible')

    def test_runtime_corruption_is_incompatible(self):
        self.facts['artifacts'][self.profile['tested_runtime'][0]] = 'e' * 64
        self.assertEqual(self.report()['status'], 'incompatible')

    def test_added_loadable_library_not_in_profile_invalidates_receipt(self):
        self.facts['private_inventory']['files']['build/native-prefix/lib/x86_64-linux-gnu/libgio-2.0.so.0'] = 'c' * 64
        self.assertEqual(self.report()['status'], 'incompatible')

    def test_gateway_python_change_invalidates_same_binary_receipt(self):
        self.facts['private_inventory']['files']['takeover/login_gateway.py'] = 'd' * 64
        self.assertEqual(self.report()['status'], 'incompatible')

    def test_removed_unlisted_file_or_symlink_change_invalidates_receipt(self):
        name = 'build/native-prefix/lib/x86_64-linux-gnu/plugin.so'
        for before, after in (({'files': {name: 'a' * 64}, 'symlinks': {}},
                               {'files': {}, 'symlinks': {}}),
                              ({'files': {name: 'a' * 64}, 'symlinks': {name + '.0': 'plugin.so'}},
                               {'files': {name: 'a' * 64}, 'symlinks': {name + '.0': 'other.so'}})):
            with self.subTest(after=after):
                self.facts['manifest']['private_inventory'] = before
                self.facts['private_inventory'] = after
                self.assertEqual(self.report()['status'], 'incompatible')

    def test_old_partial_inventory_manifest_cannot_verify(self):
        self.facts['manifest'].pop('private_inventory')
        self.assertEqual(self.report()['status'], 'incompatible')

    def test_wrong_typelib_or_missing_export_is_incompatible(self):
        name = next(iter(self.facts['typelibs']))
        self.facts['typelibs'][name] = False
        self.assertEqual(self.report()['status'], 'incompatible')
        self.facts['typelibs'][name] = True
        self.facts['elf'][self.profile['tested_runtime'][0]]['exports'] = []
        self.assertEqual(self.report()['status'], 'incompatible')

    def test_fake_unit_receipt_or_skipped_case_cannot_verify(self):
        self.facts['receipt']['scope'] = 'unit'
        self.assertEqual(self.report()['status'], 'needs-validation')
        self.facts['receipt']['scope'] = 'target-shell-integration'
        self.facts['receipt']['cases']['target-shell-startup'] = 'skipped'
        self.assertEqual(self.report()['status'], 'needs-validation')

    def test_receipt_requires_actual_binary_hashes_and_this_manifest(self):
        self.facts['receipt']['tested_binaries'] = {}
        self.assertEqual(self.report()['status'], 'needs-validation')
        self.facts['receipt']['manifest_sha256'] = 'f' * 64
        self.assertEqual(self.report()['status'], 'needs-validation')

    def test_source_baseline_or_profile_tampering_is_incompatible(self):
        self.facts['manifest']['profile_sha256'] = 'f' * 64
        self.assertEqual(self.report()['status'], 'incompatible')
        self.facts['manifest']['profile_sha256'] = self.facts['profile_sha256']
        self.facts['manifest']['sources']['mutter'] = '0' * 40
        self.assertEqual(self.report()['status'], 'incompatible')

    def test_moved_prefix_requires_explicit_installer_stage_and_cannot_make_env(self):
        self.facts['paths'] = compat.runtime_paths('/opt/staging', self.profile)
        self.assertEqual(self.report()['status'], 'incompatible')
        self.facts['expected_runtime_prefix'] = '/opt/fixture'
        self.facts['staged'] = True
        report = self.report()
        self.assertEqual(report['status'], 'verified')
        with self.assertRaises(compat.CompatibilityError):
            compat.private_environment(report)

    def test_development_owner_never_authorizes_production_env(self):
        self.facts['production_eligible'] = False
        with self.assertRaises(compat.CompatibilityError):
            compat.private_environment(self.report())


class ReadOnlyParsers(unittest.TestCase):
    def test_inventory_covers_unlisted_files_and_link_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = {'paths': {'native_prefix': 'native', 'grd_prefix': 'grd'}}
            for name in ('native', 'grd'):
                (root / name).mkdir()
                (root / name / 'data').write_bytes(b'data')
            (root / 'native/lib').mkdir()
            library = root / 'native/lib/libgio-2.0.so.0'
            library.write_bytes(b'additional private dependency')
            link = root / 'native/lib/libgio.so'
            link.symlink_to('libgio-2.0.so.0')
            inventory = compat.private_inventory(root, profile, os.getuid())
            self.assertIn('native/lib/libgio-2.0.so.0', inventory['files'])
            self.assertEqual(inventory['symlinks']['native/lib/libgio.so'], 'libgio-2.0.so.0')
            (root / 'native/lib/same-bytes.so').write_bytes(library.read_bytes())
            link.unlink()
            link.symlink_to('same-bytes.so')
            self.assertNotEqual(inventory, compat.private_inventory(root, profile, os.getuid()))
            link.unlink()
            link.symlink_to(root / 'native/data')
            with self.assertRaisesRegex(compat.CompatibilityError, 'relative|escapes'):
                compat.private_inventory(root, profile, os.getuid())
            link.unlink()
            link.symlink_to('../../grd/data')
            with self.assertRaisesRegex(compat.CompatibilityError, 'escapes'):
                compat.private_inventory(root, profile, os.getuid())

    def test_symlink_cannot_leave_anchor_then_return_through_another_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            safe = root / 'safe'
            safe.mkdir()
            (safe / 'real').write_text('fixture')
            (root / 'hop').symlink_to('safe/real')
            (safe / 'resource').symlink_to('../hop')
            self.assertEqual((safe / 'resource').resolve(), safe / 'real')
            with self.assertRaisesRegex(compat.CompatibilityError, 'escapes'):
                compat.trusted_read(safe / 'resource', safe, os.getuid())
    def test_static_elf_needed_soname_and_architecture(self):
        data = bytearray(768)
        data[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', data, 18, 62)
        struct.pack_into('<QQ', data, 32, 64, 0)
        struct.pack_into('<HHHH', data, 54, 56, 2, 0, 0)
        struct.pack_into('<IIQQQQQQ', data, 64, 1, 4, 0, 0, 0, 768, 768, 1)
        struct.pack_into('<IIQQQQQQ', data, 120, 2, 4, 192, 192, 0, 80, 80, 1)
        names = b'\0libmutter-18.so.0\0libfixture.so\0'
        data[512:512 + len(names)] = names
        for pos, pair in enumerate(((5, 512), (10, len(names)), (1, 1), (14, 19), (0, 0))):
            struct.pack_into('<qQ', data, 192 + pos * 16, *pair)
        parsed = compat.elf_metadata(bytes(data))
        self.assertEqual(parsed['needed'], ['libmutter-18.so.0'])
        self.assertEqual(parsed['soname'], 'libfixture.so')
        self.assertEqual(parsed['machine'], 62)
        with self.assertRaises(compat.CompatibilityError):
            compat.elf_metadata(bytes(data[:100]))

    def test_symlink_escape_and_writable_path_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            safe = root / 'safe'
            safe.mkdir()
            resource = safe / 'resource'
            resource.write_text('public data')
            self.assertEqual(compat.trusted_read(resource, safe, os.getuid()), b'public data')
            outside = root / 'outside'
            outside.write_text('not in candidate')
            resource.unlink()
            resource.symlink_to(outside)
            with self.assertRaisesRegex(compat.CompatibilityError, 'escapes'):
                compat.trusted_read(resource, safe, os.getuid())
            resource.unlink()
            resource.write_text('public')
            resource.chmod(0o666)
            with self.assertRaisesRegex(compat.CompatibilityError, 'Writable'):
                compat.trusted_read(resource, safe, os.getuid())

    def test_wrong_owner_is_rejected_without_chown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_text('fixture')
            original = Path.lstat
            def owner(path):
                info = original(path)
                if path == source:
                    values = list(info)
                    values[4] = 424242
                    return os.stat_result(values)
                return info
            with patch.object(Path, 'lstat', owner), self.assertRaisesRegex(compat.CompatibilityError, 'owner'):
                compat.trusted_read(source, root, os.getuid())

    def test_inspector_fails_closed_for_missing_metadata_without_loading_code(self):
        with tempfile.TemporaryDirectory() as directory:
            report = compat.inspect_candidate(Path(directory), required_owner=os.getuid())
            self.assertEqual(report['status'], 'incompatible')
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
